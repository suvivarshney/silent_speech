import os
import sys
import numpy as np
import logging
import subprocess

import torch
from torch import nn
import torch.nn.functional as F

from read_emg import EMGDataset, SizeAwareSampler
from wavenet_model import WavenetModel, save_output as save_wavenet_output
from align import align_from_distances
from asr import evaluate
from transformer import TransformerEncoderLayer
from data_utils import phoneme_inventory, decollate_tensor

from absl import flags
FLAGS = flags.FLAGS
flags.DEFINE_integer('model_size', 768, 'number of hidden dimensions')
flags.DEFINE_integer('num_layers', 6, 'number of layers')
flags.DEFINE_integer('batch_size', 1, 'training batch size')
flags.DEFINE_float('learning_rate', 1e-3, 'learning rate')
flags.DEFINE_integer('learning_rate_patience', 5, 'learning rate decay patience')
flags.DEFINE_integer('learning_rate_warmup', 500, 'steps of linear warmup')
flags.DEFINE_string('start_training_from', None, 'start training from this model')
flags.DEFINE_float('data_size_fraction', 1, 'fraction of training data to use')
flags.DEFINE_boolean('no_session_embed', False, "don't use a session embedding")
flags.DEFINE_float('phoneme_loss_weight', 0.1, 'weight of auxiliary phoneme prediction loss')
flags.DEFINE_float('l2', 1e-7, 'weight decay')

class ResBlock(nn.Module):
    def __init__(self, num_ins, num_outs, stride=1):
        super().__init__()

        self.conv1 = nn.Conv1d(num_ins, num_outs, 3, padding=1, stride=stride)
        self.bn1 = nn.BatchNorm1d(num_outs)
        self.conv2 = nn.Conv1d(num_outs, num_outs, 3, padding=1)
        self.bn2 = nn.BatchNorm1d(num_outs)

        if stride != 1 or num_ins != num_outs:
            self.residual_path = nn.Conv1d(num_ins, num_outs, 1, stride=stride)
            self.res_norm = nn.BatchNorm1d(num_outs)
        else:
            self.residual_path = None

    def forward(self, x):
        input_value = x

        x = F.relu(self.bn1(self.conv1(x)))
        x = self.bn2(self.conv2(x))

        if self.residual_path is not None:
            res = self.res_norm(self.residual_path(input_value))
        else:
            res = input_value

        return F.relu(x + res)

class Model(nn.Module):
    def __init__(self, num_ins, num_outs, num_aux_outs, num_sessions):
        super().__init__()

        self.conv_blocks = nn.Sequential(
            ResBlock(8, FLAGS.model_size, 2),
            ResBlock(FLAGS.model_size, FLAGS.model_size, 2),
            ResBlock(FLAGS.model_size, FLAGS.model_size, 2),
        )
        self.w_raw_in = nn.Linear(FLAGS.model_size, FLAGS.model_size)

        if not FLAGS.no_session_embed:
            emb_size = 32
            self.session_emb = nn.Embedding(num_sessions, emb_size)
            self.w_emb = nn.Linear(emb_size, FLAGS.model_size)

        encoder_layer = TransformerEncoderLayer(d_model=FLAGS.model_size, nhead=8, relative_positional=True, relative_positional_distance=100, dim_feedforward=3072)
        self.transformer = nn.TransformerEncoder(encoder_layer, FLAGS.num_layers)
        self.w_out = nn.Linear(FLAGS.model_size, num_outs)
        self.w_aux = nn.Linear(FLAGS.model_size, num_aux_outs)

    def forward(self, x_feat, x_raw, session_ids):
        # x shape is (batch, time, electrode)

        x_raw = x_raw.transpose(1,2) # put channel before time for conv
        x_raw = self.conv_blocks(x_raw)
        x_raw = x_raw.transpose(1,2)
        x_raw = self.w_raw_in(x_raw)

        if FLAGS.no_session_embed:
            x = x_raw
        else:
            emb = self.session_emb(session_ids)
            x = x_raw + self.w_emb(emb)

        x = x.transpose(0,1) # put time first
        x = self.transformer(x)
        x = x.transpose(0,1)
        return self.w_out(x), self.w_aux(x)

def test(model, testset, device):
    model.eval()

    dataloader = torch.utils.data.DataLoader(testset, batch_size=1, collate_fn=testset.collate_fixed_length)
    losses = []
    accuracies = []
    phoneme_confusion = np.zeros((len(phoneme_inventory),len(phoneme_inventory)))
    with torch.no_grad():
        for example in dataloader:
            X = example['emg'].to(device)
            X_raw = example['raw_emg'].to(device)
            sess = example['session_ids'].to(device)

            pred, phoneme_pred = model(X, X_raw, sess)

            loss, phon_acc = dtw_loss(pred, phoneme_pred, example, True, phoneme_confusion)
            losses.append(loss.item())

            accuracies.append(phon_acc)

    model.train()
    return np.mean(losses), np.mean(accuracies), phoneme_confusion #TODO size-weight average

def save_output(model, datapoint, filename, device, gold_mfcc=False):
    model.eval()
    if gold_mfcc:
        y = datapoint['audio_features']
    else:
        with torch.no_grad():
            sess = torch.tensor(datapoint['session_ids'], device=device).unsqueeze(0)
            X = torch.tensor(datapoint['emg'], dtype=torch.float32, device=device).unsqueeze(0)
            X_raw = torch.tensor(datapoint['raw_emg'], dtype=torch.float32, device=device).unsqueeze(0)

            pred, _ = model(X, X_raw, sess)
            pred = pred.squeeze(0)

            y = pred.cpu().detach().numpy()

    wavenet_model = WavenetModel(y.shape[1]).to(device)
    assert FLAGS.pretrained_wavenet_model is not None
    wavenet_model.load_state_dict(torch.load(FLAGS.pretrained_wavenet_model))
    save_wavenet_output(wavenet_model, y, filename, device)
    model.train()

def dtw_loss(predictions, phoneme_predictions, example, phoneme_eval=False, phoneme_confusion=None):
    device = predictions.device
    predictions = decollate_tensor(predictions, example['lengths'])
    phoneme_predictions = decollate_tensor(phoneme_predictions, example['lengths'])

    audio_features = example['audio_features'].to(device)

    phoneme_targets = example['phonemes']

    audio_features = decollate_tensor(audio_features, example['audio_feature_lengths'])

    losses = []
    correct_phones = 0
    total_length = 0
    for pred, y, pred_phone, y_phone, silent in zip(predictions, audio_features, phoneme_predictions, phoneme_targets, example['silent']):
        assert len(pred.size()) == 2 and len(y.size()) == 2
        y_phone = y_phone.to(device)

        if silent:
            dists = torch.cdist(pred.unsqueeze(0), y.unsqueeze(0))
            costs = dists.squeeze(0)

            # pred_phone (seq1_len, 48), y_phone (seq2_len)
            # phone_probs (seq1_len, seq2_len)
            pred_phone = F.log_softmax(pred_phone, -1)
            phone_lprobs = pred_phone[:,y_phone]

            costs = costs + FLAGS.phoneme_loss_weight * -phone_lprobs

            alignment = align_from_distances(costs.T.cpu().detach().numpy())

            loss = costs[alignment,range(len(alignment))].sum()

            if phoneme_eval:
                alignment = align_from_distances(costs.T.cpu().detach().numpy())

                pred_phone = pred_phone.argmax(-1)
                correct_phones += (pred_phone[alignment] == y_phone).sum().item()

                for p, t in zip(pred_phone[alignment].tolist(), y_phone.tolist()):
                    phoneme_confusion[p, t] += 1
        else:
            assert y.size(0) == pred.size(0)

            dists = F.pairwise_distance(y, pred)

            assert len(pred_phone.size()) == 2 and len(y_phone.size()) == 1
            phoneme_loss = F.cross_entropy(pred_phone, y_phone, reduction='sum')
            loss = dists.cpu().sum() + FLAGS.phoneme_loss_weight * phoneme_loss.cpu()

            if phoneme_eval:
                pred_phone = pred_phone.argmax(-1)
                correct_phones += (pred_phone == y_phone).sum().item()

                for p, t in zip(pred_phone.tolist(), y_phone.tolist()):
                    phoneme_confusion[p, t] += 1

        losses.append(loss)
        total_length += y.size(0)

    return sum(losses)/total_length, correct_phones/total_length

def train_model(trainset, devset, device, save_sound_outputs=True, n_epochs=80):
    if FLAGS.data_size_fraction >= 1:
        training_subset = trainset
    else:
        training_subset = torch.utils.data.Subset(trainset, list(range(int(len(trainset)*FLAGS.data_size_fraction))))
    dataloader = torch.utils.data.DataLoader(training_subset, pin_memory=(device=='cuda'), collate_fn=devset.collate_fixed_length, num_workers=1, batch_sampler=SizeAwareSampler(trainset, 64000))

    n_phones = len(phoneme_inventory)
    model = Model(devset.num_features, devset.num_speech_features, n_phones, devset.num_sessions).to(device)

    if FLAGS.start_training_from is not None:
        state_dict = torch.load(FLAGS.start_training_from)
        del state_dict['session_emb.weight']
        model.load_state_dict(state_dict, strict=False)

    optim = torch.optim.AdamW(model.parameters(), weight_decay=FLAGS.l2)
    lr_sched = torch.optim.lr_scheduler.ReduceLROnPlateau(optim, 'min', 0.5, patience=FLAGS.learning_rate_patience)

    def set_lr(new_lr):
        for param_group in optim.param_groups:
            param_group['lr'] = new_lr

    target_lr = FLAGS.learning_rate
    def schedule_lr(iteration):
        iteration = iteration + 1
        if iteration <= FLAGS.learning_rate_warmup:
            set_lr(iteration*target_lr/FLAGS.learning_rate_warmup)

    batch_idx = 0
    for epoch_idx in range(n_epochs):
        losses = []
        for example in dataloader:
            optim.zero_grad()
            schedule_lr(batch_idx)

            X = example['emg'].to(device)
            X_raw = example['raw_emg'].to(device)
            sess = example['session_ids'].to(device)

            pred, phoneme_pred = model(X, X_raw, sess)

            loss, _ = dtw_loss(pred, phoneme_pred, example)
            losses.append(loss.item())

            loss.backward()
            optim.step()

            batch_idx += 1
        train_loss = np.mean(losses)
        val, phoneme_acc, _ = test(model, devset, device)
        lr_sched.step(val)
        logging.info(f'finished epoch {epoch_idx+1} - validation loss: {val:.4f} training loss: {train_loss:.4f} phoneme accuracy: {phoneme_acc*100:.2f}')
        torch.save(model.state_dict(), os.path.join(FLAGS.output_directory,'model.pt'))
        if save_sound_outputs:
            save_output(model, devset[0], os.path.join(FLAGS.output_directory, f'epoch_{epoch_idx}_output.wav'), device)

    model.load_state_dict(torch.load(os.path.join(FLAGS.output_directory,'model.pt'))) # re-load best parameters

    if save_sound_outputs:
        for i, datapoint in enumerate(devset):
            save_output(model, datapoint, os.path.join(FLAGS.output_directory, f'example_output_{i}.wav'), device)

    evaluate(devset, FLAGS.output_directory)

    return model

def main():
    os.makedirs(FLAGS.output_directory, exist_ok=True)
    logging.basicConfig(handlers=[
            logging.FileHandler(os.path.join(FLAGS.output_directory, 'log.txt'), 'w'),
            logging.StreamHandler()
            ], level=logging.INFO, format="%(message)s")

    logging.info(subprocess.run(['git','rev-parse','HEAD'], stdout=subprocess.PIPE, universal_newlines=True).stdout)
    logging.info(subprocess.run(['git','diff'], stdout=subprocess.PIPE, universal_newlines=True).stdout)

    logging.info(sys.argv)

    trainset = EMGDataset(dev=False,test=False)
    devset = EMGDataset(dev=True)
    logging.info('output example: %s', devset.example_indices[0])
    logging.info('train / dev split: %d %d',len(trainset),len(devset))

    device = 'cuda' if torch.cuda.is_available() and not FLAGS.debug else 'cpu'

    model = train_model(trainset, devset, device, save_sound_outputs=(FLAGS.pretrained_wavenet_model is not None))

if __name__ == '__main__':
    FLAGS(sys.argv)
    main()
