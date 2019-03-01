import torch
from torch import optim
import torch.nn as nn
import torch.nn.functional as F
import global_variables
from torch.optim.lr_scheduler import ReduceLROnPlateau

import bleu_score


class BagOfWords(nn.Module):
   def init_layers(self):
       for l in self.layers:
           if getattr(l, 'weight', None) is not None:
               torch.nn.init.xavier_uniform_(l.weight)

   def __init__(self, input_size, hidden_size=512, reduce='sum', nlayers=2, activation='ReLU', dropout=0.1, batch_norm=False):
       super(BagOfWords, self).__init__()

       self.emb_dim = hidden_size
       self.reduce = reduce
       self.nlayers = nlayers
       self.hidden_size = hidden_size

       self.activation = getattr(nn, activation)

       self.embedding = nn.EmbeddingBag(num_embeddings=input_size, embedding_dim=self.emb_dim, mode=reduce)

       if batch_norm is True:
           self.batch_norm = nn.BatchNorm1d(self.emb_dim)
       self.layers = nn.ModuleList([nn.Linear(self.emb_dim, self.hidden_size)])

       self.layers.append(self.activation())
       self.layers.append(nn.Dropout(p=dropout))
       for i in range(self.nlayers-2):
           self.layers.append(nn.Linear(self.hidden_size, self.hidden_size))
           self.layers.append(self.activation())
           self.layers.append(nn.Dropout(p=dropout))
       self.layers.append(nn.Linear(self.hidden_size, self.hidden_size))
       self.init_layers()

   def forward(self, x):
       postemb = self.embedding(x)
       if hasattr(self, 'batch_norm'):
           x = self.batch_norm(postemb)
       else:
           x = postemb
       for l in self.layers:
           x = l(x)

       return x, None


class EncoderRNN(nn.Module):
    """Encodes the input context."""

    def __init__(self, input_size, hidden_size, numlayers):
        """Initialize encoder.
        :param input_size: size of embedding
        :param hidden_size: size of GRU hidden layers
        :param numlayers: number of GRU layers
        """
        super().__init__()
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(input_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size, num_layers=numlayers,
                          batch_first=True)

    def forward(self, input, hidden=None):
        """Return encoded state.
        :param input: (batchsize x seqlen) tensor of token indices.
        :param hidden: optional past hidden state
        """
        embedded = self.embedding(input)
        output, hidden = self.gru(embedded, hidden)
        return output, hidden



class DecoderRNN(nn.Module):
    """Generates a sequence of tokens in response to context."""

    def __init__(self, output_size, hidden_size, numlayers):
        """Initialize decoder.
        :param input_size: size of embedding
        :param hidden_size: size of GRU hidden layers
        :param numlayers: number of GRU layers
        """
        super().__init__()
        self.hidden_size = hidden_size

        self.embedding = nn.Embedding(output_size, hidden_size)
        self.gru = nn.GRU(hidden_size, hidden_size, num_layers=numlayers,
                          batch_first=True)
        self.out = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim=2)

    def forward(self, input, hidden):
        """Return encoded state.
        :param input: batch_size x 1 tensor of token indices.
        :param hidden: past (e.g. encoder) hidden state
        """
        emb = self.embedding(input)
        rel = F.relu(emb)
        output, hidden = self.gru(rel, hidden)
        scores = self.softmax(self.out(output))
        return scores, hidden


class seq2seq(nn.Module):

    def __init__(self, encoder, decoder, lr = 1e-3, use_cuda = True, 
                        hiddensize = 128, numlayers = 2, target_lang = None, longest_label = 20, 
                        clip = 0.3):
        super(seq2seq, self).__init__()

        device = torch.device("cuda" if (torch.cuda.is_available() and use_cuda) else "cpu")
        self.device = device;
        self.encoder = encoder.to(device);
        self.decoder = decoder.to(device)

        self.target_lang = target_lang;

        self.bl = bleu_score.BLEU_SCORE()

        # set up the criterion
        self.criterion = nn.NLLLoss()

        # set up optims for each module
        # self.optims = {
        #     'encoder': optim.SGD(encoder.parameters(), lr=lr, nesterov=True, momentum = 0.99),
        #     'decoder': optim.SGD(decoder.parameters(), lr=lr, nesterov=True, momentum = 0.99)
        # }

        self.optims = {
             'nmt': optim.SGD(self.parameters(), lr=lr, nesterov=True, momentum = 0.99)
        }

        self.scheduler = {};
        for x in self.optims.keys():
            self.scheduler[x] = ReduceLROnPlateau(self.optims[x], mode = 'max', min_lr=1e-4,  patience=0, verbose = True);

        self.longest_label = longest_label
        self.hiddensize = hiddensize
        self.numlayers = numlayers
        self.clip = clip;
        self.START = torch.LongTensor([global_variables.SOS_token]).to(device)
        self.END_IDX = global_variables.EOS_token;


    def zero_grad(self):
        """Zero out optimizer."""
        for optimizer in self.optims.values():
            optimizer.zero_grad()

    def update_params(self):
        """Do one optimization step."""
        if self.clip is not None:
            torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), self.clip)
            torch.nn.utils.clip_grad_norm_(self.decoder.parameters(), self.clip)
        for optimizer in self.optims.values():
            optimizer.step()

    def scheduler_step(self, val_bleu):
        for scheduler in self.scheduler.values():
            scheduler.step(val_bleu)


    def v2t(self, vector):
        """Convert vector to text.
        :param vector: tensor of token indices.
            1-d tensors will return a string, 2-d will return a list of strings
        """
        if vector.dim() == 1:
            output_tokens = []
            # Remove the final END_TOKEN that is appended to predictions
            for token in vector:
                if token == self.END_IDX:
                    break
                else:
                    output_tokens.append(token)
            return self.target_lang.vec2txt(output_tokens)

        elif vector.dim() == 2:
            return [self.v2t(vector[i]) for i in range(vector.size(0))]
        raise RuntimeError('Improper input to v2t with dimensions {}'.format(
            vector.size()))

    def get_bleu_score(self, val_loader):
        predicted_list = []
        real_list = []

        for data in val_loader:
            predicted_list += self.eval_step(data);
            real_list += self.v2t(data.label_vec);

        return self.bl.corpus_bleu(predicted_list, [real_list])[0]

    def train_step(self, batch):
        """Train model to produce ys given xs.
        :param batch: parlai.core.torch_agent.Batch, contains tensorized
                      version of observations.
        Return estimated responses, with teacher forcing on the input sequence
        (list of strings of length batchsize).
        """
        xs, ys = batch.text_vec, batch.label_vec

        if xs is None:
            return
        xs = xs.to(self.device)
        ys = ys.to(self.device)

        bsz = xs.size(0)
        starts = self.START.expand(bsz, 1)  # expand to batch size
        loss = 0
        self.zero_grad()
        self.encoder.train()
        self.decoder.train()
        target_length = ys.size(1)
        # save largest seen label for later
        self.longest_label = max(target_length, self.longest_label)

        _encoder_output, encoder_hidden = self.encoder(xs)

        # Teacher forcing: Feed the target as the next input
        y_in = ys.narrow(1, 0, ys.size(1) - 1)
        decoder_input = torch.cat([starts, y_in], 1)
        decoder_output, decoder_hidden = self.decoder(decoder_input,
                                                      encoder_hidden)

        scores = decoder_output.view(-1, decoder_output.size(-1))
        loss = self.criterion(scores, ys.view(-1))
        loss.backward()
        self.update_params()

        _max_score, predictions = decoder_output.max(2)
        return self.v2t(predictions), loss.item() 

    def eval_step(self, batch):
        """Generate a response to the input tokens.
        :param batch: parlai.core.torch_agent.Batch, contains tensorized
                      version of observations.
        Return predicted responses (list of strings of length batchsize).
        """
        xs = batch.text_vec

        if xs is None:
            return

        xs = xs.to(self.device)

        bsz = xs.size(0)
        starts = self.START.expand(bsz, 1)  # expand to batch size
        # just predict
        self.encoder.eval()
        self.decoder.eval()
        _encoder_output, encoder_hidden = self.encoder(xs)

        predictions = []
        done = [False for _ in range(bsz)]
        total_done = 0
        decoder_input = starts
        decoder_hidden = encoder_hidden

        for _ in range(self.longest_label):
            # generate at most longest_label tokens
            decoder_output, decoder_hidden = self.decoder(decoder_input,
                                                          decoder_hidden)
            _max_score, preds = decoder_output.max(2)
            predictions.append(preds)
            decoder_input = preds  # set input to next step

            # check if we've produced the end token
            for b in range(bsz):
                if not done[b]:
                    # only add more tokens for examples that aren't done
                    if preds[b].item() == self.END_IDX:
                        # if we produced END, we're done
                        done[b] = True
                        total_done += 1
            if total_done == bsz:
                # no need to generate any more
                break
        predictions = torch.cat(predictions, 1)
        return self.v2t(predictions)
