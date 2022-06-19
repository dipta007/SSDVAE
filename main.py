########################################
#   module for training the DAVAE model
#
#
########################################
import torch
import torch.nn as nn
from torchtext.data import Iterator as BatchIter
from  torch import distributions
from show_inf import *
import argparse
import numpy as np
import random
import math
from torch.autograd import Variable
from sklearn import metrics
from EncDec import Encoder, Decoder, Attention, fix_enc_hidden, kl_divergence
import torch.nn.functional as F
import data_utils as du
from SSDVAE import SSDVAE
from DAG import example_tree
from masked_cross_entropy import masked_cross_entropy
from data_utils import EOS_TOK, MAX_EVAL_SEQ_LEN, MIN_EVAL_SEQ_LEN, SOS_TOK, PAD_TOK
import time
from torchtext.vocab import GloVe
from report_md import *
import pickle
import gc
import glob
import sys
import os
from utility import print_repo_info, wandb_log
from wiki_val_generate import do_ranking
import wandb

wandb_dict = {}

def tally_parameters(model):
    n_params = sum([p.nelement() for p in model.parameters()])
    print('* number of parameters: %d' % n_params)
    
def get_scores_for_frame(model, y_true):
    def preprocess(frames):
        # print(frames)
        frames = list(filter(lambda x: x > 2, frames))
        frames = list(set(frames))
        return frames

    def get_score_for_instance(y_true, y_pred):
        precision = len(set(y_true).intersection(set(y_pred))) / (
            1 if len(y_pred) == 0 else len(y_pred)
        )
        recall = len(set(y_true).intersection(set(y_pred))) / (
            1 if len(y_true) == 0 else len(y_true)
        )
        f1 = (2 * precision * recall) / (
            1 if (precision + recall) == 0 else (precision + recall)
        )
        return precision * 100.0, recall * 100.0, f1 * 100.0

    latent_gumbels = model.latent_gumbels
    y_pred = torch.argmax(latent_gumbels, dim=-1)

    precision, recall, f1 = 0.0, 0.0, 0.0
    for y, y_p in zip(y_true, y_pred):
        n_pre, n_re, n_f1 = get_score_for_instance(
            preprocess(y.tolist()), preprocess(y_p.tolist())
        )
        precision += n_pre
        recall += n_re
        f1 += n_f1

    precision /= y_true.size(0)
    recall /= y_true.size(0)
    f1 /= y_true.size(0)
    return precision, recall, f1


def monolithic_compute_loss(iteration, model, target, target_lens, latent_values, latent_root, diff, dec_outputs, use_cuda, args, train=True,topics_dict=None,real_sentence=None,next_frames_dict=None,word_to_frame=None,template_dict=None,show=False, true_f_vals=None):
    """
    use this function for validation loss. NO backprop in this function.
    """
    logits = model.logits.transpose(0,1).contiguous() # convert to [batch, seq, vocab]
    q_log_q= model.q_log_q
    frame_classifier = model.frame_classifier
    frame_classifier_total = -frame_classifier.sum((1,2)).mean()
    q_log_q_total= q_log_q.sum(-1).mean()
    precision, recall, f1 = get_scores_for_frame(model, true_f_vals)

    if use_cuda:
        ce_loss = masked_cross_entropy(logits, Variable(target.cuda()), Variable(target_lens.cuda()))
    else:
        ce_loss = masked_cross_entropy(logits, Variable(target), Variable(target_lens))

    loss = ce_loss + q_log_q_total + frame_classifier_total
    if train==True and show==True:
        print_iter_stats(iteration, loss, ce_loss, q_log_q_total,topics_dict,real_sentence,next_frames_dict,frame_classifier_total,word_to_frame,template_dict,args,show=True)
        
    global wandb_dict
    pre_label = "train" if train else "val"
    pre = {}
    if not train:
        pre = wandb_dict
    wandb_dict[f"{pre_label}/total_loss"] = loss.item()
    wandb_dict[f"{pre_label}/loss"] = (
        pre.get(f"{pre_label}/loss", 0.0) + loss.item()
    )
    wandb_dict[f"{pre_label}/ce_loss"] = (
        pre.get(f"{pre_label}/ce_loss", 0.0) + ce_loss.item()
    )
    wandb_dict[f"{pre_label}/kl_loss"] = (
        pre.get(f"{pre_label}/kl_loss", 0.0) + q_log_q_total.item()
    )
    wandb_dict[f"{pre_label}/fcls_loss"] = (
        pre.get(f"{pre_label}/fcls_loss", 0.0)
        + frame_classifier_total.item()
    )
    wandb_dict[f"{pre_label}/precision"] = (
        pre.get(f"{pre_label}/precision", 0.0) + precision
    )
    wandb_dict[f"{pre_label}/recall"] = (
        pre.get(f"{pre_label}/recall", 0.0) + recall
    )
    wandb_dict[f"{pre_label}/f1"] = (
        pre.get(f"{pre_label}/f1", 0.0) + f1
    )
    
    return loss, ce_loss # tensor




def print_iter_stats(iteration, loss, ce_loss, q_log_q_total,topics_dict,real_sentence,next_frames_dict,frame_classifier_total,word_to_frame,template_dict,args,show=False):
    if iteration%10==0:
        print("Iteration: ", iteration)
        print("Total: ", loss.cpu().item())
        print("CE: ", ce_loss.cpu().item())
        print("q_log_q_total: ",q_log_q_total.cpu().item())
        print("frame_classifier_total: ",frame_classifier_total.cpu().item())
        print('-'*50)
        if False:
            print("sentence: "," ".join(real_sentence))
            topics_to_md('chain: ',topics_dict)
            templates=np.arange(args.template).reshape((-1,5))
            topics_to_md('words: ',word_to_frame)
            print('-'*50)

def get_wiki_inv_score(args, train, models, vocab, vocab2, pre=""):
    data_mode = "train" if train else "valid"
    valid_narr = "./data/wiki_inv/obs_{}_0.6_TUP_DIST.txt".format(data_mode)
    dataset_wiki = du.NarrativeClozeDataset(
        valid_narr,
        vocab,
        src_seq_length=MAX_EVAL_SEQ_LEN,
        min_seq_length=MIN_EVAL_SEQ_LEN,
        LM=False,
    )
    wiki_data_len = len(dataset_wiki)
    print(f"{pre}ranking_dataset: ", wiki_data_len)
    # Batch size during decoding is set to 1
    wiki_batches = BatchIter(
        dataset_wiki, 1, sort_key=lambda x: len(x.actual), train=False, device=-1
    )
    wiki_acc = do_ranking(
        args,
        models,
        wiki_batches,
        vocab,
        wiki_data_len,
        True,
    )

    pre_label = "train" if train else "val"
    global wandb_dict
    wandb_dict[f"{pre_label}/{pre}wiki_inv"] = wiki_acc

    return wiki_acc


def check_save_model_path(save_model):
    save_model_path = os.path.abspath(save_model)
    model_dirname = os.path.dirname(save_model_path)
    if not os.path.exists(model_dirname):
        os.makedirs(model_dirname)



def classic_train(args,args_dict,args_info):
    global wandb_dict
    """
    Train the model in the ol' fashioned way, just like grandma used to
    Args
        args (argparse.ArgumentParser)
    """
    if args.cuda and torch.cuda.is_available():
        print("Using cuda")
        use_cuda = True
    elif args.cuda and not torch.cuda.is_available():
        print("You do not have CUDA, turning cuda off")
        use_cuda = False
    else:
        use_cuda=False

    #Load the data
    print("\nLoading Vocab")
    print('args.vocab: ',args.vocab)
    vocab , verb_max_idx = du.load_vocab(args.vocab)
    print("Vocab Loaded, Size {}".format(len(vocab.stoi.keys())))
    print(vocab.itos[:40])
    args_dict["vocab"]=len(vocab.stoi.keys())
    vocab2 = du.load_vocab(args.frame_vocab_address,is_Frame=True)
    print(vocab2.itos[:40])
    print("Frames-Vocab Loaded, Size {}".format(len(vocab2.stoi.keys())))
    total_frames=len(vocab2.stoi.keys())
    args.total_frames=total_frames
    args.num_latent_values=args.total_frames
    print('total frames: ',args.total_frames)

    # import wandb
    experiment_name = '{}_eps_{}_num_{}_seed_{}'.format('chain_event',str(args_dict['obsv_prob']),str(args_dict['exp_num']),str(args_dict['seed']))

    if args.use_pretrained:
        pretrained = GloVe(name='6B', dim=args.emb_size, unk_init=torch.Tensor.normal_)
        vocab.load_vectors(pretrained)
        print("Vectors Loaded")

    print("Loading Dataset")
    dataset = du.SentenceDataset(path=args.train_data,path2=args.train_frames,
    vocab=vocab,vocab2=vocab2,num_clauses=args.num_clauses, add_eos=False,is_ref=True,obsv_prob=args.obsv_prob)

    print("Finished Loading Dataset {} examples".format(len(dataset)))
    batches = BatchIter(dataset, args.batch_size, sort_key=lambda x:len(x.text), train=True, sort_within_batch=True, device=-1)
    data_len = len(dataset)

    if args.load_model:
        print("Loading the Model")
        model = torch.load(args.load_model)
    else:
        print("Creating the Model")
        bidir_mod = 2 if args.bidir else 1
        latents = example_tree(args.num_latent_values, (bidir_mod*args.enc_hid_size, args.latent_dim),
                               frame_max=args.total_frames,padding_idx=vocab2.stoi['<pad>'],use_cuda=use_cuda, nohier_mode=args.nohier) #assume bidirectional

        hidsize = (args.enc_hid_size, args.dec_hid_size)
        model = SSDVAE(args.emb_size, hidsize, vocab, latents, layers=args.nlayers, use_cuda=use_cuda,
                      pretrained=args.use_pretrained, dropout=args.dropout,frame_max=args.total_frames,
                      template=args.template,latent_dim=args.latent_dim,verb_max_idx=verb_max_idx)



    #create the optimizer
    if args.load_opt:
        print("Loading the optimizer state")
        optimizer = torch.load(args.load_opt)
    else:
        print("Creating the optimizer anew")
        optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    start_time = time.time() #start of epoch 1
    curr_epoch = 1
    valid_loss = [0.0]
    min_ppl=1e10
    print("Loading Validation Dataset.")
    val_dataset = du.SentenceDataset(path=args.valid_data,path2=args.valid_frames,
    vocab=vocab,vocab2=vocab2,num_clauses=args.num_clauses, add_eos=False,is_ref=True,obsv_prob=0.0,print_valid=True)

    print("Finished Loading Validation Dataset {} examples.".format(len(val_dataset)))
    val_batches = BatchIter(val_dataset, args.batch_size, sort_key=lambda x:len(x.text), train=False, sort_within_batch=True, device=-1)
    for idx,item in enumerate(val_batches):
        if idx==0:
            break
        token_rev=[vocab.itos[int(v.numpy())] for v in item.target[0][-1]]
        frame_rev=[vocab2.itos[int(v.numpy())] for v in item.frame[0][-1]]
        ref_frame=[vocab2.itos[int(v.numpy())] for v in item.ref[0][-1]]

        print('token_rev:',token_rev,len(token_rev),"lengths: ",item.target[1][-1])
        print('frame_rev:',frame_rev,len(frame_rev),"lengths: ",item.frame[1][-1])
        print('ref_frame:',ref_frame,len(ref_frame),"lengths: ",item.ref[1][-1])
        print('-'*50)
    print('Model_named_params:{}'.format(model.named_parameters()))

    for iteration, bl in enumerate(batches): #this will continue on forever (shuffling every epoch) till epochs finished
        wandb_dict = {}
        wandb_dict["epoch"] = curr_epoch
        wandb_dict["iteration"] = iteration
        
        batch, batch_lens = bl.text
        f_vals,f_vals_lens = bl.frame
        target, target_lens = bl.target
        f_ref, _ = bl.ref

        if use_cuda:
            batch = Variable(batch.cuda())
            f_vals= Variable(f_vals.cuda())
        else:
            batch = Variable(batch)
            f_vals= Variable(f_vals)

        model.zero_grad()
        latent_values, latent_root, diff, dec_outputs = model(batch, batch_lens,f_vals=f_vals)

        topics_dict,real_sentence,next_frames_dict,word_to_frame,template_dict=show_inference(model,batch,vocab,vocab2,f_vals,f_ref,args)
        loss, _ = monolithic_compute_loss(iteration, model, target, target_lens, latent_values, latent_root,
                                          diff, dec_outputs, use_cuda, args=args,topics_dict=topics_dict,real_sentence=real_sentence,next_frames_dict=next_frames_dict,
                                          word_to_frame=word_to_frame,template_dict=template_dict,train=True,show=True, true_f_vals=f_ref)

        # backward propagation
        loss.backward()
        # Gradient clipping
        torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
        # Optimize
        optimizer.step()

        # End of an epoch - run validation
        if iteration%10==0:
            print("\nFinished Training Epoch/iteration {}/{}".format(curr_epoch, iteration))
            # do validation
            valid_logprobs=0.0
            valid_lengths=0.0
            valid_loss = 0.0
            valid_batch_total_size = 0
            with torch.no_grad():
                for v_iteration, bl in enumerate(val_batches):
                    batch, batch_lens = bl.text
                    f_vals,f_vals_lens = bl.frame
                    target, target_lens = bl.target
                    f_ref, _ = bl.ref
                    batch_lens = batch_lens.cpu()
                    if use_cuda:
                        batch = Variable(batch.cuda())
                        f_vals = Variable(f_vals.cuda())
                    else:
                        batch = Variable(batch)
                        f_vals = Variable(f_vals)
                    latent_values, latent_root, diff, dec_outputs = model(batch, batch_lens,f_vals=f_vals)
                    topics_dict,real_sentence,next_frames_dict,word_to_frame,template_dict=show_inference(model,batch,vocab,vocab2,f_vals,f_ref,args)
                    loss, ce_loss = monolithic_compute_loss(iteration, model, target, target_lens, latent_values, latent_root,
                                                    diff, dec_outputs, use_cuda, args=args,topics_dict=topics_dict,real_sentence=real_sentence,next_frames_dict=next_frames_dict,
                                                    word_to_frame=word_to_frame,template_dict=template_dict,train=False,show=False,  true_f_vals=f_ref)

                    valid_loss = valid_loss + ce_loss.data.clone()
                    valid_logprobs+=ce_loss.data.clone().cpu().numpy()*target_lens.sum().cpu().data.numpy()
                    valid_lengths+=target_lens.sum().cpu().data.numpy()
                    # print("valid_lengths: ",valid_lengths[0])
                    valid_batch_total_size += 1

            nll=valid_logprobs/valid_lengths
            ppl=np.exp(nll)
            
            wandb_dict["val/nll"] = nll
            wandb_dict["val/ppl"] = ppl
            wandb_dict[f"val/loss"] /= valid_batch_total_size
            wandb_dict[f"val/ce_loss"] /= valid_batch_total_size
            wandb_dict[f"val/kl_loss"] /= valid_batch_total_size
            wandb_dict[f"val/fcls_loss"] /= valid_batch_total_size
            wandb_dict[f"val/precision"] /= valid_batch_total_size
            wandb_dict[f"val/recall"] /= valid_batch_total_size
            wandb_dict[f"val/f1"] /= valid_batch_total_size
            
            valid_loss = valid_loss/(v_iteration+1)
            print("**Validation loss {:.2f}.**\n".format(valid_loss.item()))
            print("**Validation NLL {:.2f}.**\n".format(nll))
            print("**Validation PPL {:.2f}.**\n".format(ppl))
            args_dict_wandb = {"val_nll":nll,"val_ppl":ppl,"valid_loss":valid_loss}
            if ppl<min_ppl:
                min_ppl=ppl
                args_dict["min_ppl"]=min_ppl
                dir_path = os.path.dirname(os.path.realpath(__file__))
                save_file="".join(["_"+str(key)+"_"+str(value) for key,value in args_dict.items() if key != "min_ppl"])
                args_to_md(model="chain",args_dict=args_dict)
                model_path=os.path.join(dir_path+"/saved_models/chain_"+save_file+".pt")
                torch.save(model,model_path)
                config_path=os.path.join(dir_path+"/saved_configs/chain_"+save_file+".pkl")
                with open (config_path, "wb") as f:
                    pickle.dump((args_dict,args_info),f)
            print('\t==> min_ppl {:4.4f} '.format(min_ppl))
            
            get_wiki_inv_score(args, False, model, vocab, vocab2)
        
        wandb_log(wandb_dict)



if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='DAVAE')
    parser.add_argument('--train_data', type=str)
    parser.add_argument('--valid_data', type=str)
    parser.add_argument("--wandb_project", default='deleteme', type=str, help="wandb project")
    parser.add_argument("--sh_file", default=None, type=str, help="The shell script running this python file.")
    parser.add_argument('--vocab', type=str, help='the vocabulary pickle file')
    parser.add_argument('--emb_size', type=int, default=300, help='size of word embeddings')
    parser.add_argument('--enc_hid_size', type=int, default=512, help='size of encoder hidden')
    parser.add_argument('--dec_hid_size', type=int, default=512, help='size of encoder hidden')
    parser.add_argument('--nlayers', type=int, default=2, help='number of layers')
    parser.add_argument('--lr', type=float, default=0.001, help='initial learning rate')
    parser.add_argument('--log_every', type=int, default=200)
    parser.add_argument('--save_after', type=int, default=500)
    parser.add_argument('--validate_after', type=int, default=2500)
    parser.add_argument('--optimizer', type=str, default='adam', help='adam, adagrad, sgd')
    parser.add_argument('--clip', type=float, default=5.0, help='gradient clipping')
    parser.add_argument('--epochs', type=int, default=40, help='upper epoch limit')
    parser.add_argument('--batch_size', type=int, default=200, metavar='N', help='batch size')
    parser.add_argument('--seed', type=int, default=11, help='random seed')
    parser.add_argument('--cuda', action='store_true', help='use CUDA')
    parser.add_argument('--bidir', type=bool, default=True, help='Use bidirectional encoder')
    # parser.add_argument('-src_seq_length', type=int, default=50, help="Maximum source sequence length")
    parser.add_argument('-max_decode_len', type=int, default=50, help='Maximum prediction length.')
    parser.add_argument('-save_model', default='model', help="""Model filename""")
    parser.add_argument('-num_latent_values', type=int, default=400, help='How many values for each categorical value')
    parser.add_argument('-latent_dim', type=int, default=512, help='The dimension of the latent embeddings')
    parser.add_argument('-use_pretrained', type=bool, default=True, help='Use pretrained glove vectors')
    parser.add_argument('-commit_c', type=float, default=0.25, help='loss hyperparameters')
    parser.add_argument('-commit2_c', type=float, default=0.15, help='loss hyperparameters')
    parser.add_argument('-dropout', type=float, default=0.0, help='loss hyperparameters')
    parser.add_argument('--load_model', type=str)
    parser.add_argument('--num_clauses', type=int,default=5)
    parser.add_argument('--load_opt', type=str)
    parser.add_argument('--nohier', action='store_true', help='use the nohier model instead')
    parser.add_argument('--frame_max', type=int, default=500)
    parser.add_argument('--obsv_prob', type=float, default=1.0,help='the percentage of observing frames')
    parser.add_argument('--template', type=int, default=20)
    parser.add_argument('--exp_num', type=int, default=1)
    parser.add_argument('--max_decode', type=int, default=10, help="""max sentences to be evaluated/decoded.""")


    args = parser.parse_args()
    path = os.path.dirname(os.path.realpath(__file__))
    args.model='chain'
    args.command = ' '.join(sys.argv)

    args.train_data='./data/train_0.6_TUP.txt'
    args.train_frames='./data/train_0.6_frame.txt'

    args.valid_data='./data/valid_0.6_TUP.txt'
    args.valid_frames='./data/valid_0.6_frame.txt'

    args.test_data='./data/test_0.6_TUP.txt'
    args.vocab='./data/vocab_40064_verb_max_13572.pkl'
    args.frame_vocab_address='./data/vocab_frame_'+str(args.frame_max)+'.pkl'
    args.frame_vocab_ref='./data/vocab_frame_all.pkl'
    
    print_repo_info()


    args.latent_dim=args.frame_max
    args.num_latent_values=args.frame_max
    args_info={}
    for arg in vars(args):
        args_info[arg] = getattr(args, arg)
    print('parser_info:')
    for item in args_info:
        print(item,": ",args_info[item])
    print('-'*50)



    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)


    if torch.cuda.is_available():
        if not args.cuda:
            print("WARNING: You have a CUDA device, so you should probably run with --cuda")
        else:
            torch.cuda.manual_seed(args.seed)

    keys=["model","emb_size","nlayers",
         "lr","batch_size","num_clauses","num_latent_values",
         "latent_dim","dropout","bidir","use_pretrained","obsv_prob","template","frame_max","exp_num","seed"]
    args_dict={key:str(value) for key,value in vars(args).items() if key in keys}

    experiment_name = f"NAACL_{args_dict['exp_num']}"
    wandb.init(
        project="SSDVAE_ext",
        entity="ssdvae-hierarchical",
        name=experiment_name,
        config=args_info,
    )
    
    classic_train(args,args_dict,args_info)



