#!/bin/bash
cd /home/ubuntu/cryptominisat
git checkout -f remotes/origin/master
git branch -D master
git checkout -b master
git pull origin master
/home/ubuntu/cryptominisat/scripts/aws/client.py --host 172.30.0.242 > /home/ubuntu/log.txt &