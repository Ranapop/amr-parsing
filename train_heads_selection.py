from typing import Dict
import time

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.nn.functional import pad as torch_pad
from torch.nn.functional import binary_cross_entropy_with_logits
from torch.utils.tensorboard import SummaryWriter

from data_pipeline.data_reading import get_paths
from data_pipeline.vocab import Vocabs
import data_pipeline.dataset
from data_pipeline.dataset import PAD, EOS, UNK, PAD_IDX
from data_pipeline.dataset import AMRDataset

from models import HeadsSelection

BATCH_SIZE = 32
DEV_BATCH_SIZE = 32
NO_EPOCHS = 3
HIDDEN_SIZE = 40

UNK_REL_LABEL = ':unk-label'

def compute_loss(vocabs: Vocabs, mask: torch.Tensor,
                 logits: torch.Tensor, gold_outputs: torch.Tensor):
  """
  Args:
    vocabs: Vocabs object (with the 3 vocabs).
    mask: Mask for weighting the loss.
    logits: Concepts edges scores (batch size, seq len, seq len).
    gold_outputs: Gold adj mat (with relation labels) of shape
      (batch size, seq len, seq len).

  Returns:
    Binary cross entropy loss over batch.
  """
  no_rel_index = vocabs.relation_vocab[None]
  pad_idx = vocabs.relation_vocab[PAD]
  binary_outputs = (gold_outputs != no_rel_index) * (gold_outputs != pad_idx)
  binary_outputs = binary_outputs.type(torch.FloatTensor)
  weights = mask.type(torch.FloatTensor)
  flattened_logits = logits.flatten()
  flattened_binary_outputs = binary_outputs.flatten()
  flattened_weights = weights.flatten()
  loss = binary_cross_entropy_with_logits(
    flattened_logits, flattened_binary_outputs, flattened_weights)
  return loss

def eval_step(model, batch):
  inputs = batch['concepts']
  inputs_lengths = batch['concepts_lengths']
  gold_adj_mat = batch['adj_mat']
  amr_ids = batch['amr_id']

  optimizer.zero_grad()
  logits = model(inputs, inputs_lengths)
  seq_len = inputs.shape[0]
  mask = HeadsSelection.create_mask(seq_len, inputs_lengths, False)
  loss = compute_loss(vocabs, mask, logits, gold_adj_mat)
  return loss

def get_logged_examples(data_loader: DataLoader):
  # Dummy impl.
  #This could be a few AMR examples (gold & predictions).
  first_batch = next(iter(data_loader))
  amr_ids = first_batch['amr_id']
  logged_text = ' '.join(amr_ids)
  return logged_text

def evaluate_model(model: nn.Module,
                   data_loader: DataLoader):
  model.eval()
  with torch.no_grad():
    epoch_loss = 0
    no_batches = 0
    for batch in data_loader:
      loss = eval_step(model, batch)
      epoch_loss += loss
      no_batches += 1
    epoch_loss = epoch_loss / no_batches
    logged_text = get_logged_examples(data_loader)
    return epoch_loss, logged_text

def train_step(model: nn.Module,
               optimizer,
               vocabs,
               batch: Dict[str, torch.Tensor]):
  inputs = batch['concepts']
  inputs_lengths = batch['concepts_lengths']
  gold_adj_mat = batch['adj_mat']

  optimizer.zero_grad()
  logits = model(inputs, inputs_lengths, gold_adj_mat)
  seq_len = inputs.shape[0]
  mask = HeadsSelection.create_mask(seq_len, inputs_lengths, True, gold_adj_mat)
  loss = compute_loss(vocabs, mask, logits, gold_adj_mat)
  loss.backward()
  optimizer.step()
  return loss

def train_model(model: nn.Module,
                optimizer,
                vocabs,
                writer,
                train_data_loader: DataLoader,
                dev_data_loader: DataLoader):
  model.train()
  for epoch in range(NO_EPOCHS):
    start_time = time.time()
    i = 0
    epoch_loss = 0
    no_batches = 0
    for batch in train_data_loader:
      batch_loss = train_step(model, optimizer, vocabs, batch)
      epoch_loss += batch_loss
      no_batches += 1
    epoch_loss = epoch_loss / no_batches
    dev_loss, logged_text = evaluate_model(model, dev_data_loader)
    model.train()
    end_time = time.time()
    time_passed = end_time - start_time 
    print('Epoch {} (took {:.2f} seconds)'.format(epoch+1, time_passed))
    print('Train loss: {}, dev loss: {} '.format(epoch_loss, dev_loss))
    writer.add_scalar("Loss/train", epoch_loss, epoch)
    writer.add_scalar("Loss/eval", dev_loss, epoch)
    writer.add_text('amr', logged_text, epoch)

if __name__ == "__main__":

  device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
  print('Training on device', device)

  # subsets = ['bolt', 'cctv', 'dfa', 'dfb', 'guidelines',
            #  'mt09sdl', 'proxy', 'wb', 'xinhua']
  subsets = ['bolt']
  train_paths = get_paths('training', subsets)
  dev_paths = get_paths('dev', subsets)

  special_words = ([PAD, EOS, UNK], [PAD, EOS, UNK], [PAD, UNK, None])
  vocabs = Vocabs(train_paths, UNK, special_words, min_frequencies=(1, 1, 1))
  train_dataset = AMRDataset(
    train_paths, vocabs, device, seq2seq_setting=False, ordered=True)
  dev_dataset = AMRDataset(
    dev_paths, vocabs, device, seq2seq_setting=False, ordered=True)
  
  train_data_loader = DataLoader(
    train_dataset, batch_size=BATCH_SIZE, collate_fn=train_dataset.collate_fn)
  dev_data_loader = DataLoader(
    dev_dataset, batch_size=DEV_BATCH_SIZE, collate_fn=dev_dataset.collate_fn)

  model = HeadsSelection(vocabs.concept_vocab_size, HIDDEN_SIZE).to(device)
  optimizer = optim.Adam(model.parameters())
  
  writer = SummaryWriter("temp/heads_selection")
  train_model(model, 
    optimizer, vocabs, writer, train_data_loader, dev_data_loader)
  writer.close()