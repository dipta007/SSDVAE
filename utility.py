import os
import random
import subprocess
import sys

import numpy as np
import torch

import wandb
from wandb import AlertLevel

ADA = True


def seed_everything(seed=42):
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True
    torch.cuda.manual_seed_all(seed)


def tally_parameters(model):
    n_params = sum([p.nelement() for p in model.parameters()])
    print("* number of parameters: %d" % n_params)


def check_save_model_path(save_model):
    save_model_path = os.path.abspath(save_model)
    model_dirname = os.path.dirname(save_model_path)
    if not os.path.exists(model_dirname):
        os.makedirs(model_dirname)


def get_repo_name():
    repo_name = os.path.basename(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"])
        .strip()
        .decode()
    )
    return repo_name


def get_commit_hash():
    return subprocess.check_output(["git", "describe", "--always"]).strip().decode()


def print_repo_info():
    print("*" * 44)
    print(" ".join(sys.argv))
    print("\n")
    git_commit_hash = get_commit_hash()
    repo_name = get_repo_name()
    print(f"Git repo: {repo_name}")
    print(f"Git commit hash: {git_commit_hash}")
    print("*" * 44)


def wandb_alert(title, text="", level=AlertLevel.INFO):
    try:
        wandb.alert(get_repo_name() + " - " + title, text, level)
    except Exception as e:
        pass


def wandb_log(wandb_dict):
    try:
        wandb.log(wandb_dict)
    except Exception as e:
        # print(e, file=sys.stderr)
        pass