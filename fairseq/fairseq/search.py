# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math

import numpy as np
import torch
import torch.nn as nn
from fairseq.data.data_utils import collate_tokens


class Search(nn.Module):
    def __init__(self, tgt_dict):
        super().__init__()
        self.pad = tgt_dict.pad()
        self.unk = tgt_dict.unk()
        self.eos = tgt_dict.eos()
        self.vocab_size = len(tgt_dict)
        self.src_lengths = torch.tensor(-1)
        self.scores_buf = torch.Tensor()
        self.indices_buf = torch.Tensor().long()
        self.beams_buf = torch.Tensor().long()

    @torch.jit.export
    def _init_buffers(self, t):
        if not self.scores_buf.size()[0]:
            self.scores_buf = torch.empty(0).to(t)
            self.indices_buf = torch.empty(0).to(t).long()
            self.beams_buf = torch.empty(0).to(t).long()

    def step(self, step, lprobs, scores):
        """Take a single search step.

        Args:
            step: the current search step, starting at 0
            lprobs: (bsz x input_beam_size x vocab_size)
                the model's log-probabilities over the vocabulary at the current step
            scores: (bsz x input_beam_size x step)
                the historical model scores of each hypothesis up to this point

        Return: A tuple of (scores, indices, beams) where:
            scores: (bsz x output_beam_size)
                the scores of the chosen elements; output_beam_size can be
                larger than input_beam_size, e.g., we may return
                2*input_beam_size to account for EOS
            indices: (bsz x output_beam_size)
                the indices of the chosen elements
            beams: (bsz x output_beam_size)
                the hypothesis ids of the chosen elements, in the range [0, input_beam_size)
        """
        raise NotImplementedError

    @torch.jit.export
    def set_src_lengths(self, src_lengths):
        self.src_lengths = src_lengths


class BeamSearch(Search):
    def __init__(self, tgt_dict):
        super().__init__(tgt_dict)

    @torch.jit.export
    def step(self, step: int, lprobs, scores):
        self._init_buffers(lprobs)
        bsz, beam_size, vocab_size = lprobs.size()

        if step == 0:
            # at the first step all hypotheses are equally likely, so use
            # only the first beam
            lprobs = lprobs[:, ::beam_size, :].contiguous()
        else:
            # make probs contain cumulative scores for each hypothesis
            lprobs.add_(scores[:, :, step - 1].unsqueeze(-1))

        top_prediction = torch.topk(
            lprobs.view(bsz, -1),
            k=min(
                # Take the best 2 x beam_size predictions. We'll choose the first
                # beam_size of these which don't predict eos to continue with.
                beam_size * 2,
                lprobs.view(bsz, -1).size(1) - 1,  # -1 so we never select pad
            ),
        )
        self.scores_buf = top_prediction[0]
        self.indices_buf = top_prediction[1]
        self.beams_buf = torch.div(self.indices_buf, vocab_size)
        self.indices_buf.fmod_(vocab_size)
        return self.scores_buf, self.indices_buf, self.beams_buf


class LengthConstrainedBeamSearch(Search):

    def __init__(self, tgt_dict, min_len_a, min_len_b, max_len_a, max_len_b):
        super().__init__(tgt_dict)
        self.min_len_a = min_len_a
        self.min_len_b = min_len_b
        self.max_len_a = max_len_a
        self.max_len_b = max_len_b
        self.beam = BeamSearch(tgt_dict)

    def step(self, step, lprobs, scores):
        min_lens = self.min_len_a * self.src_lengths + self.min_len_b
        max_lens = self.max_len_a * self.src_lengths + self.max_len_b
        lprobs[step < min_lens, :, self.eos] = -math.inf
        lprobs[step == max_lens, :, self.eos] = 0
        lprobs[step > max_lens, :, self.eos] = -math.inf
        return self.beam.step(step, lprobs, scores)


class DiverseBeamSearch(Search):
    """Diverse Beam Search.

    See "Diverse Beam Search: Decoding Diverse Solutions from Neural Sequence
    Models" for details.

    We only implement the Hamming Diversity penalty here, which performed best
    in the original paper.
    """

    def __init__(self, tgt_dict, num_groups, diversity_strength):
        super().__init__(tgt_dict)
        self.num_groups = num_groups
        self.diversity_strength = -diversity_strength
        self.diversity_buf = None
        self.beam = BeamSearch(tgt_dict)

    def step(self, step, lprobs, scores):
        super()._init_buffers(lprobs)
        bsz, beam_size, vocab_size = lprobs.size()
        if beam_size % self.num_groups != 0:
            raise ValueError(
                'DiverseBeamSearch requires --beam to be divisible by the number of groups'
            )

        # initialize diversity penalty
        if self.diversity_buf is None:
            self.diversity_buf = lprobs.new()
        torch.zeros(lprobs[:, 0, :].size(), out=self.diversity_buf)

        scores_G, indices_G, beams_G = [], [], []
        for g in range(self.num_groups):
            lprobs_g = lprobs[:, g::self.num_groups, :]
            scores_g = scores[:, g::self.num_groups, :] if step > 0 else None

            # apply diversity penalty
            if g > 0:
                lprobs_g = torch.add(lprobs_g, self.diversity_strength, self.diversity_buf.unsqueeze(1))
            else:
                lprobs_g = lprobs_g.contiguous()

            scores_buf, indices_buf, beams_buf = self.beam.step(step, lprobs_g, scores_g)
            beams_buf.mul_(self.num_groups).add_(g)

            scores_G.append(scores_buf.clone())
            indices_G.append(indices_buf.clone())
            beams_G.append(beams_buf.clone())

            # update diversity penalty
            self.diversity_buf.scatter_add_(
                1,
                indices_buf,
                self.diversity_buf.new_ones(indices_buf.size())
            )

        # interleave results from different groups
        self.scores_buf = torch.stack(scores_G, dim=2, out=self.scores_buf).view(bsz, -1)
        self.indices_buf = torch.stack(indices_G, dim=2, out=self.indices_buf).view(bsz, -1)
        self.beams_buf = torch.stack(beams_G, dim=2, out=self.beams_buf).view(bsz, -1)
        return self.scores_buf, self.indices_buf, self.beams_buf


class Sampling(Search):

    def __init__(self, tgt_dict, sampling_topk=-1, sampling_topp=-1.0):
        super().__init__(tgt_dict)
        self.sampling_topk = sampling_topk
        self.sampling_topp = sampling_topp

    def _sample_topp(self, lprobs):
        """Sample among the smallest set of elements whose cumulative probability mass exceeds p.

        See `"The Curious Case of Neural Text Degeneration"
        (Holtzman et al., 2019) <https://arxiv.org/abs/1904.09751>`_.

        Args:
            lprobs: (bsz x input_beam_size x vocab_size)
                the model's log-probabilities over the vocabulary at the current step

        Return: A tuple of (trimed_probs, truncated_indices) where:
            trimed_probs: (bsz x input_beam_size x ?)
                the model's probabilities over the elements selected to sample from. The
                width of the third dimension is determined by top-P.
            truncated_indices: (bsz x input_beam_size x ?)
                the indices of the chosen elements.
        """
        probs = lprobs.exp_()

        # sort the last dimension (vocab dimension) in descending order
        sorted_probs, sorted_indices = probs.sort(descending=True)

        # compute a mask to indicate the words to be included in the top-P set.
        cumsum_probs = sorted_probs.cumsum(dim=2)
        mask = cumsum_probs.lt(self.sampling_topp)

        # note that mask was computed by 'lt'. One more word needs to be included
        # so that the cumulative probability mass can exceed p.
        cumsum_mask = mask.cumsum(dim=2)
        last_included = cumsum_mask[:, :, -1:]
        last_included.clamp_(0, mask.size()[2] - 1)
        mask = mask.scatter_(2, last_included, 1)

        # truncate unnecessary dims.
        max_dim = last_included.max()
        truncated_mask = mask[:, :, :max_dim + 1]
        truncated_probs = sorted_probs[:, :, :max_dim + 1]
        truncated_indices = sorted_indices[:, :, :max_dim + 1]

        # trim the words that are not in top-P by setting their probabilities
        # to 0, so that they would not be sampled later.
        trim_mask = (~truncated_mask)
        trimed_probs = truncated_probs.masked_fill_(trim_mask, 0)
        return trimed_probs, truncated_indices

    def step(self, step, lprobs, scores, src_tokens=None, gen_tokens=None, **kwargs): # src and tgt_tokens to far #TODO make sure that we get an empty tgt tokens on first pass
        
        super()._init_buffers(lprobs)
        bsz, beam_size, vocab_size = lprobs.size()
        logprob = None

        # use kwargs to init discriminator stuff
        rescore = kwargs.get("rescore", "False")
        coefs = kwargs.get("coefs", [])
        scorers = kwargs.get("scorers", [])
        learn = kwargs.get("learn", "False")
        learn_every_token = kwargs.get("learn_every_token")
        coef_trainer = kwargs.get("coef_trainer")
        gold_tokens = kwargs.get("gold_tokens")
        gold_lm_score = kwargs.get("gold_lprobs")
        gen_lm_score = kwargs.get("gen_lprobs")

        if step == 0:
            # at the first step all hypotheses are equally likely, so use
            # only the first beam
            lprobs = lprobs[:, ::beam_size, :].contiguous()

        if self.sampling_topp > 0:
            # only sample from the smallest set of words whose cumulative probability mass exceeds p
            probs, top_indices = self._sample_topp(lprobs)
        elif self.sampling_topk > 0:
            # only sample from top-k candidates
            lprobs, top_indices = lprobs.topk(self.sampling_topk)
            logprob = lprobs.clone()
            probs = lprobs.exp_()
        else:
            probs = lprobs.exp_()

        lprobs = logprob.clone()
        #sample
        #print("initial lprobs, probs")
        #print(lprobs, probs)
        if rescore and step > 0:
            # potentially rescore
            # TODO support batch > 1 by chunking tensors
            top_indices = top_indices.squeeze(0) # make 2D, unclear why 3
            n_hypos = top_indices.shape[1]
            score_adjustment = np.zeros(n_hypos)
            #cont_tokens = todo make this work for multiple batch size  > 1 by chunking tensors
            if learn and learn_every_token:
                coefs = coef_trainer.weight_model.coefs.weight.data.cpu().squeeze().numpy()
                if not coefs.shape: # numpy makes single element arrays shapeless which makes them not iterable
                    coefs = [coefs.item()]
                if step % 100 == 0:
                    print("Coefs: {}".format(coefs))
            all_raw_scores = []
            for coef, scorer in zip(coefs, scorers):
                # this makes an array for each scorer from calling the scorer forward function on the candidate tokens

                # assemble hypothesis batch
                all_tokens = torch.cat((src_tokens, gen_tokens), dim=1) if step > 0 else src_tokens  # add the stuff generated so far to the fixed prefix
                all_tokens = all_tokens.repeat_interleave(n_hypos, dim=0) # repeat by k of topk
                #breakpoint()
                hypothesis_batch = torch.cat((all_tokens, top_indices.transpose(0, 1)), dim=1) # builds a bunch of examples of src + cont toks
                
                if hypothesis_batch.shape[1] > 512: # roberta can't take more than 512 tokens
                    hypothesis_batch = hypothesis_batch[:,:-512]
                gold_separate = False
                if learn and learn_every_token:  # add the gold example to the end as new row
                    gold_example = torch.cat((src_tokens, gold_tokens), dim=1)
                    if gold_example.shape[1] == hypothesis_batch.shape[1]:  # this will always be true unless the generation has become longer than the gold
                        hypothesis_batch = torch.cat((hypothesis_batch, gold_example))
                    else:
                        gold_separate = True

                #hypothesis_batch = collate_tokens([torch.cat((src_tokens, top_indices[i])) for i in range(len(top_indices))], pad_idx=1)
                # returns a tensor of scores
                new_scores = scorer.predict("sentence_classification_head", hypothesis_batch) # determine whether to norm scores

                if learn and gold_separate and learn_every_token:
                    gold_scores = scorer.predict("sentence_classification_head", gold_example)
                    #raw_gold_scores = np.array([gs[1].data.item() for gs in gold_scores]) #for score in new_scores]) # index 1 is positive class
                    new_scores = torch.cat((new_scores, gold_scores))
                raw_scores = np.array([score[1].data.item() for score in new_scores]) # index 1 is positive class
                all_raw_scores.append(raw_scores)
                # elementwise add the new scores to the np array after elementwise multiplying by coef
                score_adjustment += raw_scores[:self.sampling_topk] * coef  # truncate so don't include extra stuff like gold scores if in there
                #if learn and gold_separate:
                #    new_scores = scorer.predict("sentence_classification_head", gold_example)
                #    raw_scores = np.array([score[1].data.item() for score in new_scores]) # index 1 is positive class
                #    all_raw_scores.append(raw_scores)
            #print([scores.shape for scores in all_raw_scores])
            all_raw_scores = np.stack(all_raw_scores, axis=-1)  # this converts to num_candidates x num_scorers so each row is all adjusted scores for a candidate. Probs necessary only for proper beam search

            #if self.learn and num_cont_words < len(true_cont_tokens): # tgt_tokens should be the true cont tokens

            #print("Score Adjustment")
            #print(score_adjustment)
            #print("Before Disc")
            #print(lprobs, probs)

            mod_probs = lprobs.clone()
            for i in range(n_hypos): # unclear again why lprobs is 3D
                mod_probs[0][0][i] = lprobs[0][0][i] + score_adjustment[i]
            #print("After Disc")
            lprobs = mod_probs.clone()
            probs = mod_probs.clone().exp_()
            #print(lprobs, probs)
            max_lprob, max_idx = lprobs.max(2)  # along second dimension
            if learn and learn_every_token:
                next_gen_lm_score = torch.sum(torch.cat((max_lprob[0], gen_lm_score.unsqueeze(0))))
                gold_cont_raw_scores = all_raw_scores[-1]
                #train coefficients with lm score of gold, best candidate score, and continuation scores for gold
                loss = coef_trainer.train_coefficients(gold_lm_score, next_gen_lm_score,
                                                       gold_cont_raw_scores,
                                                       all_raw_scores[max_idx.data.item()])


        # sample
        if step == 0:
            self.indices_buf = torch.multinomial(
                probs.view(bsz, -1),
                beam_size,
                replacement=True,
                out=self.indices_buf,
            ).view(bsz, beam_size)
        else:
            self.indices_buf = torch.multinomial(
                probs.view(bsz * beam_size, -1),
                1,
                replacement=True,
                out=self.indices_buf,
            ).view(bsz, beam_size)

        if step == 0:
            # expand to beam size
            probs = probs.expand(bsz, beam_size, -1)

        # gather scores
        self.scores_buf = torch.gather(
            probs,
            dim=2,
            index=self.indices_buf.unsqueeze(-1),
            out=self.scores_buf,
        )
        self.scores_buf = self.scores_buf.log_().view(bsz, -1)

        # remap indices if using top-k or top-P sampling
        if self.sampling_topk > 0 or self.sampling_topp > 0:
            self.indices_buf = torch.gather(
                top_indices.expand(bsz, beam_size, -1),
                dim=2,
                index=self.indices_buf.unsqueeze(-1),
            ).squeeze(2)

        if step == 0:
            self.beams_buf = self.indices_buf.new_zeros(bsz, beam_size)
        else:
            self.beams_buf = torch.arange(0, beam_size, out=self.beams_buf).repeat(bsz, 1)
            # make scores cumulative
            self.scores_buf.add_(
                torch.gather(
                    scores[:, :, step - 1],
                    dim=1,
                    index=self.beams_buf,
                )
            )

        return self.scores_buf, self.indices_buf, self.beams_buf


class DiverseSiblingsSearch(Search):
    """
    Beam search with diverse siblings.

    See "A Simple, Fast Diverse Decoding Algorithm for Neural Generation" for details.
    https://arxiv.org/abs/1611.08562

    1/ Calculate hypotheses for each beam
    2/ Intra-sibling ordering
    3/ Rewrite scores
    4/ Choose top K hypotheses

    if diversity_rate == 0 is equivalent to BeamSearch
    """

    def __init__(self, tgt_dict, diversity_rate):
        super().__init__(tgt_dict)
        self.diversity_rate = diversity_rate
        self.beam = BeamSearch(tgt_dict)

    def step(self, step, lprobs, scores):
        super()._init_buffers(lprobs)
        bsz, beam_size, vocab_size = lprobs.size()
        k = min(
            # Take the best 2 x beam_size predictions. We'll choose the first
            # beam_size of these which don't predict eos to continue with.
            beam_size * 2,
            lprobs.view(bsz, -1).size(1) - 1,  # -1 so we never select pad
        )
        s_list = [lprobs.new() for i in range(beam_size)]
        i_list = [torch.LongTensor().to(device=lprobs.device) for i in range(beam_size)]
        sibling_score = lprobs.new(range(1, k + 1)) * self.diversity_rate

        if step == 0:
            return self.beam.step(step, lprobs, scores)
        lprobs.add_(scores[:, :, step - 1].unsqueeze(-1))

        # 1/ Calculate hypotheses for each beam
        for i in range(beam_size):
            torch.topk(lprobs[:, i, :].view(bsz, -1), k, out=(s_list[i], i_list[i]))
            i_list[i].fmod_(vocab_size)

            # 2/ Intra-sibling ordering by default from topk + 3/ Rewrite scores
            s_list[i].sub_(sibling_score)

        # 4/ Choose top K hypotheses
        indices = torch.stack(i_list, dim=1).view(bsz, -1)

        final_scores = lprobs.new()
        final_indices = torch.LongTensor().to(device=lprobs.device)
        final_beams = torch.LongTensor().to(device=lprobs.device)
        torch.topk(
            torch.stack(s_list, dim=1).view(bsz, -1),
            k,
            out=(final_scores, final_indices),
        )

        torch.div(final_indices, k, out=final_beams)

        for i in range(bsz):
            final_indices[i] = indices[i][final_indices[i]]

        return final_scores, final_indices, final_beams
