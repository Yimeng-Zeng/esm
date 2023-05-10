# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
from typing import Any, Dict, List, Optional, Tuple, NamedTuple
import torch
from torch import nn
from torch import Tensor
import torch.nn.functional as F
from scipy.spatial import transform

from esm.data import Alphabet

from .features import DihedralFeatures
from .gvp_encoder import GVPEncoder
from .gvp_utils import unflatten_graph
from .gvp_transformer_encoder import GVPTransformerEncoder
from .transformer_decoder import TransformerDecoder
from .util import rotate, CoordBatchConverter 


class GVPTransformerModel(nn.Module):
    """
    GVP-Transformer inverse folding model.

    Architecture: Geometric GVP-GNN as initial layers, followed by
    sequence-to-sequence Transformer encoder and decoder.
    """

    def __init__(self, args, alphabet):
        super().__init__()
        encoder_embed_tokens = self.build_embedding(
            args, alphabet, args.encoder_embed_dim,
        )
        decoder_embed_tokens = self.build_embedding(
            args, alphabet, args.decoder_embed_dim, 
        )
        encoder = self.build_encoder(args, alphabet, encoder_embed_tokens)
        decoder = self.build_decoder(args, alphabet, decoder_embed_tokens)
        self.args = args
        self.encoder = encoder
        self.decoder = decoder

    @classmethod
    def build_encoder(cls, args, src_dict, embed_tokens):
        encoder = GVPTransformerEncoder(args, src_dict, embed_tokens)
        return encoder

    @classmethod
    def build_decoder(cls, args, tgt_dict, embed_tokens):
        decoder = TransformerDecoder(
            args,
            tgt_dict,
            embed_tokens,
        )
        return decoder

    @classmethod
    def build_embedding(cls, args, dictionary, embed_dim):
        num_embeddings = len(dictionary)
        padding_idx = dictionary.padding_idx
        emb = nn.Embedding(num_embeddings, embed_dim, padding_idx)
        nn.init.normal_(emb.weight, mean=0, std=embed_dim ** -0.5)
        nn.init.constant_(emb.weight[padding_idx], 0)
        return emb

    def forward(
        self,
        coords,
        padding_mask,
        confidence,
        prev_output_tokens,
        return_all_hiddens: bool = False,
        features_only: bool = False,
    ):
        encoder_out = self.encoder(coords, padding_mask, confidence,
            return_all_hiddens=return_all_hiddens)
        logits, extra = self.decoder(
            prev_output_tokens,
            encoder_out=encoder_out,
            features_only=features_only,
            return_all_hiddens=return_all_hiddens,
        )
        return logits, extra
    
    def sample(self, coords, partial_seq=None, temperature=1.0, confidence=None, device=None, flag1=False, flag2=False, flag3=False, flag4=False):
        """
        Samples sequences based on multinomial sampling (no beam search).

        Args:
            coords: L x 3 x 3 list representing one backbone
            partial_seq: Optional, partial sequence with mask tokens if part of
                the sequence is known
            temperature: sampling temperature, use low temperature for higher
                sequence recovery and high temperature for higher diversity
            confidence: optional length L list of confidence scores for coordinates
        """
        L = len(coords)
        # Convert to batch format
        batch_converter = CoordBatchConverter(self.decoder.dictionary)
        batch_coords, confidence, _, _, padding_mask = (
            batch_converter([(coords, confidence, None)], device=device)
        )

        if flag1:
            #print("batch_coords", batch_coords.shape)
            print("batch_coords", batch_coords)
        
        # Start with prepend token
        mask_idx = self.decoder.dictionary.get_idx('<mask>')
        sampled_tokens = torch.full((1, 1+L), mask_idx, dtype=int)
        sampled_tokens[0, 0] = self.decoder.dictionary.get_idx('<cath>')
        if partial_seq is not None:
            for i, c in enumerate(partial_seq):
                sampled_tokens[0, i+1] = self.decoder.dictionary.get_idx(c)
            
        # Save incremental states for faster sampling
        incremental_state = dict()
        
        # Run encoder only once
        encoder_out = self.encoder(batch_coords, padding_mask, confidence)

        if flag2:
            print("encoder_out", encoder_out['encoder_out'][0])
        
        # Make sure all tensors are on the same device if a GPU is present
        if device:
            sampled_tokens = sampled_tokens.to(device)

        if flag3:
            print("sampled_tokens", sampled_tokens)
        
        # Decode one token at a time
        for i in range(1, L+1):
            logits, _ = self.decoder(
                sampled_tokens[:, :i], 
                encoder_out,
                incremental_state=incremental_state,
            )
            logits = logits[0].transpose(0, 1)
            logits /= temperature
            probs = F.softmax(logits, dim=-1)
            if sampled_tokens[0, i] == mask_idx:
                sampled_tokens[:, i] = torch.multinomial(probs, 1).squeeze(-1)
        sampled_seq = sampled_tokens[0, 1:]
        
        # Convert back to string via lookup
        return ''.join([self.decoder.dictionary.get_tok(a) for a in sampled_seq])

    def sample_batch(self, coords, num_samples=1, partial_seq=None, temperature=1.0, confidence=None, device=None):
        """
        Samples sequences based on multinomial sampling (no beam search).

        Args:
            coords: L x 3 x 3 list representing one backbone
            partial_seq: Optional, partial sequence with mask tokens if part of
                the sequence is known
            temperature: sampling temperature, use low temperature for higher
                sequence recovery and high temperature for higher diversity
            confidence: optional length L list of confidence scores for coordinates
        """
        L = len(coords)
        # Convert to batch format
        batch_converter = CoordBatchConverter(self.decoder.dictionary)
        batch = [(coords, confidence, None)] * num_samples
        batch_coords, confidence, _, _, padding_mask = (
            batch_converter(batch, device=device)
        )
        
        # Start with prepend token
        mask_idx = self.decoder.dictionary.get_idx('<mask>')
        sampled_tokens = torch.full((1, 1+L), mask_idx, dtype=int)
        sampled_tokens[0, 0] = self.decoder.dictionary.get_idx('<cath>')
        if partial_seq is not None:
            for i, c in enumerate(partial_seq):
                sampled_tokens[0, i+1] = self.decoder.dictionary.get_idx(c)
            
        # Save incremental states for faster sampling
        incremental_state = dict()
        
        # Run encoder only once
        all_encoder_out = self.encoder(batch_coords, padding_mask, confidence)
        
        # Make sure all tensors are on the same device if a GPU is present
        if device:
            sampled_tokens = sampled_tokens.to(device)
            # make list of duplicate tensors for each sample
            all_sampled_tokens = [sampled_tokens.clone() for _ in range(num_samples)]

        all_seqs = []

        for sample_idx in range(num_samples):

            sampled_tokens = all_sampled_tokens[sample_idx]

            # Get encoder output for this sample, sample_idx th element for each key in all_encoder_out
            encoder_out = {}
            encoder_out['encoder_out'] = [all_encoder_out['encoder_out'][0][:, sample_idx:sample_idx+1, :]]
            encoder_out['encoder_padding_mask'] = [all_encoder_out['encoder_padding_mask'][0][sample_idx:sample_idx+1, :]]
            # print(all_encoder_out['encoder_padding_mask'][0].shape)
            for key in all_encoder_out.keys():
                if key != 'encoder_out' and key != 'encoder_padding_mask':
                    try:
                        encoder_out[key] = all_encoder_out[key][sample_idx:sample_idx+1]
                    except:
                        encoder_out[key] = all_encoder_out[key]

        
            # Decode one token at a time
            for i in range(1, L+1):
                logits, _ = self.decoder(
                    sampled_tokens[:, :i], 
                    encoder_out,
                    incremental_state=incremental_state,
                )
                logits = logits[0].transpose(0, 1)
                logits /= temperature
                probs = F.softmax(logits, dim=-1)
                if sampled_tokens[0, i] == mask_idx:
                    sampled_tokens[:, i] = torch.multinomial(probs, 1).squeeze(-1)
            sampled_seq = sampled_tokens[0, 1:]
            sampled_seq = ''.join([self.decoder.dictionary.get_tok(a) for a in sampled_seq])
            all_seqs.append(sampled_seq)
        
        # Convert back to string via lookup
        return all_seqs


    def sample_batch2(self, coords, num_samples=1, partial_seq=None, temperature=1.0, confidence=None, device=None, flag1=False, flag2=False, flag3=False, flag4=False):
        """
        Samples sequences based on multinomial sampling (no beam search).

        Args:
            coords: L x 3 x 3 list representing one backbone
            partial_seq: Optional, partial sequence with mask tokens if part of
                the sequence is known
            temperature: sampling temperature, use low temperature for higher
                sequence recovery and high temperature for higher diversity
            confidence: optional length L list of confidence scores for coordinates
        """
        L = len(coords)
        # Convert to batch format
        batch_converter = CoordBatchConverter(self.decoder.dictionary)
        batch = [(coords, confidence, None)] * num_samples
        batch_coords, confidence, _, _, padding_mask = (
            batch_converter(batch, device=device)
        )
        
        # Start with prepend token
        mask_idx = self.decoder.dictionary.get_idx('<mask>')
        sampled_tokens = torch.full((1, 1+L), mask_idx, dtype=int)
        sampled_tokens[0, 0] = self.decoder.dictionary.get_idx('<cath>')
        if partial_seq is not None:
            for i, c in enumerate(partial_seq):
                sampled_tokens[0, i+1] = self.decoder.dictionary.get_idx(c)
            
        # Save incremental states for faster sampling
        incremental_state = dict()
        
        # Run encoder only once
        all_encoder_out = self.encoder(batch_coords, padding_mask, confidence)
        
        # Make sure all tensors are on the same device if a GPU is present
        if device:
            sampled_tokens = sampled_tokens.to(device)
            if flag1:
                #print("sampled_tokens", sampled_tokens)
                print("sampled_tokens.shape", sampled_tokens.shape)
            sampled_tokens = sampled_tokens.repeat(num_samples, 1)
            if flag1:
                #print("sampled_tokens", sampled_tokens)
                print("sampled_tokens.shape", sampled_tokens.shape)

        # Get encoder output for all samples
        encoder_out = {}
        encoder_out['encoder_out'] = [all_encoder_out['encoder_out'][0]]
        encoder_out['encoder_padding_mask'] = [all_encoder_out['encoder_padding_mask'][0]]
        for key in all_encoder_out.keys():
            if key != 'encoder_out' and key != 'encoder_padding_mask':
                try:
                    encoder_out[key] = all_encoder_out[key]
                except:
                    encoder_out[key] = all_encoder_out[key]

        # Decode one token at a time
        for i in range(1, L+1):
            logits, _ = self.decoder(
                sampled_tokens[:, :i], 
                encoder_out,
                incremental_state=incremental_state,
            )
            logits = logits.transpose(0, 1)
            logits /= temperature

            if flag2:
                #print("logits", logits)
                print("logits.shape", logits.shape)

            probs = F.softmax(logits, dim=-1)

            if flag3:
                #print("probs", probs)
                print("probs.shape", probs.shape)

            if flag4:
                print("mask_idx", mask_idx)
                #print("mask_idx.shape", mask_idx.shape)

            # for j in range(logits.shape[1]):  # loop over sequence length
            #     print(sampled_tokens[j:j+1, i])
            #     sampled_tokens[j:j+1, i] = torch.where(sampled_tokens[j:j+1, i] == mask_idx, 
            #                                     torch.multinomial(probs[:, j, :], 1).squeeze(-1), 
            #                                     sampled_tokens[j:j+1, i])

            for j in range(logits.shape[1]):  # loop over sequence length
                # print(sampled_tokens[j:j+1, i])
                if sampled_tokens[j:j+1, i] == mask_idx:
                    print(torch.multinomial(probs[:, j, :], 1).squeeze(-1))
                    sampled_tokens[j:j+1, i] = torch.multinomial(probs[:, j, :], 1).squeeze(-1)
            

        # Convert tokens to strings
        all_seqs = []
        for sample_idx in range(num_samples):
            sampled_seq = sampled_tokens[sample_idx, 1:]
            sampled_seq = ''.join([self.decoder.dictionary.get_tok(a) for a in sampled_seq])
            all_seqs.append(sampled_seq)

        
        # Convert back to string via lookup
        return all_seqs
