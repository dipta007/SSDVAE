#!/bin/bash

#SBATCH --job-name="run_1234_NAACL"
#SBATCH -D .
#SBATCH --output=log/output_run_1234.out
#SBATCH --error=log/error_run_1234.err
#SBATCH --nodes=1
#SBATCH --gres=gpu:1
#SBATCH --mem=100000
#SBATCH --time=72:00:00
#SBATCH --constraint=rtx_6000

v=$(git status --porcelain | wc -l)
if [[ $v -gt 1 ]]; then
    echo "Error: uncommited changes" >&2
    exit 1
else
    echo "Success: No uncommited changes"
    "${1}" "${2}" "${3}" "${4}"
fi
echo $v
