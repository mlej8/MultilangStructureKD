#! /bin/bash

export CUDA_VISIBLE_DEVICES=0
export CUDA_LAUNCH_BLOCKING=1

# confirm gpu availalable
nvidia-smi

# get current time
time="`date +%Y-%m-%d-%H-%M-%S`"

python train_with_teacher.py $@ > logs/$time.log 2>&1 &
