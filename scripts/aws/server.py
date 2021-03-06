#!/usr/bin/env python
# -*- coding: utf-8 -*-

import random
import os
import socket
import sys
import optparse
import struct
import pickle
import time
import pprint
import traceback
import subprocess
import boto

class PlainHelpFormatter(optparse.IndentedHelpFormatter):
    def format_description(self, description):
        if description:
            return description + "\n"
        else:
            return ""

usage = "usage: %prog"
parser = optparse.OptionParser(usage=usage, formatter=PlainHelpFormatter())
parser.add_option("--verbose", "-v", action="store_true"
                    , default=False, dest="verbose"
                    , help="Be more verbose"
                    )

parser.add_option("--port", "-p"
                    , default=10000, dest="port"
                    , help="Port to use", type="int"
                    )

parser.add_option("--tout", "-t"
                    , default=6000, dest="timeout_in_secs"
                    , help="Timeout for the file in seconds", type=int
                    )

parser.add_option("--extratime"
                    , default=7*60, dest="extra_time"
                    , help="Timeout for the server to send us the results", type=int
                    )

parser.add_option("--memlimit", "-m"
                    , default=1600, dest="mem_limit_in_mb"
                    , help="Memory limit in MB", type=int
                    )

parser.add_option("--cnfdir"
                    , default="satcomp14", dest="cnf_dir_name", type=str
                    , help="The list of CNF files to solve, with first line the directory"
                    )

parser.add_option("--solver"
                    , default="/home/ubuntu/cryptominisat/build/cryptominisat", dest="solver"
                    , help="Solver executable", type=str
                    )

parser.add_option("--s3bucket"
                    , default="msoos-solve-results", dest="s3_bucket"
                    , help="S3 Bucket to upload finished data", type=str
                    )

parser.add_option("--s3folder"
                    , default="results", dest="s3_folder"
                    , help="S3 folder base name to upload finished data", type=str
                    )

parser.add_option("--git"
                    , dest="git_rev", type=str
                    , help="The GIT revision to use"
                    )

parser.add_option("--opt"
                    , dest="extra_opts", type=str, default=""
                    , help="Extra options to give to solver"
                    )

parser.add_option("--noshutdown", "-n"
                    , default=False, dest="noshutdown", action="store_true"
                    , help="Do not shut down clients"
                    )


#parse options
(options, args) = parser.parse_args()

def get_revision() :
    pwd = os.getcwd()
    os.chdir('/home/ubuntu/cryptominisat')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'])
    os.chdir(pwd)
    return revision.strip()

if not options.git_rev:
    options.git_rev = get_revision()
    print "Revsion not given, taking HEAD: %s" % options.git_rev


def get_n_bytes_from_connection(sock, MSGLEN) :
    chunks = []
    bytes_recd = 0
    while bytes_recd < MSGLEN:
        chunk = sock.recv(min(MSGLEN - bytes_recd, 2048))
        if chunk == '':
            raise RuntimeError("socket connection broken")
        chunks.append(chunk)
        bytes_recd = bytes_recd + len(chunk)

    return ''.join(chunks)

class ToSolve:
    def __init__(self, num, name) :
        self.num = num
        self.name = name

class Server :
    def __init__(self) :
        self.files_available = []
        self.files_finished = []
        self.files = {}

        fnames = open(options.cnf_dir_name, "r")
        options.cnf_dir = fnames.next().strip()
        print "CNF dir really is:", options.cnf_dir
        for num, fname in zip(xrange(10000), fnames):
            fname = fname.strip()
            self.files[num] = ToSolve(num, fname)
            self.files_available.append(num)
            print "File added: ", fname
        fnames.close()

        self.files_running = {}
        print "Solving %d files" % len(self.files_available)
        self.uniq_cnt = 0

    def listen_to_connection(self) :
        # Create a TCP/IP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # Bind the socket to the port
        server_address = ('0.0.0.0', options.port)
        print >>sys.stderr, 'starting up on %s port %s' % server_address
        sock.bind(server_address)

        #Listen for incoming connections
        sock.listen(1)
        return sock

    def handle_done(self, connection, indata) :
        file_num = indata["file_num"]

        print "Finished with file %s (num %d)" % (self.files[indata["file_num"]], indata["file_num"])
        self.files_finished.append(indata["file_num"])
        if file_num in self.files_running:
            del self.files_running[file_num]

        print "Num files_available:", len(self.files_available)
        print "Num files_finished:", len(self.files_finished)
        sys.stdout.flush()

    def check_for_dead_files(self) :
        this_time = time.time()
        files_to_remove_from_files_running = []
        for file_num, starttime in self.files_running.iteritems():
            duration = this_time-starttime
            #print "* death check. running:" , file_num, " duration: ", duration
            if duration > options.timeout_in_secs + options.extra_time:
                print "* dead file " , file_num, " duration: ", duration, " re-inserting"
                files_to_remove_from_files_running.append(file_num)
                self.files_available.append(file_num)

        for c in files_to_remove_from_files_running:
            del self.files_running[c]

    def find_something_to_solve(self) :
        self.check_for_dead_files()
        print "Num files_available pre-send:", len(self.files_available)

        if len(self.files_available) == 0:
            return None

        file_num = self.files_available[0]
        del self.files_available[0]
        print "Num files_available post-send:", len(self.files_available)
        sys.stdout.flush()

        return file_num

    def handle_build(self, connection, indata) :
        tosend = {}
        tosend["solver"] = options.solver
        tosend["revision"] = options.git_rev
        tosend["noshutdown"] = options.noshutdown
        tosend = pickle.dumps(tosend)
        tosend = struct.pack('!q', len(tosend)) + tosend

        print "Sending git revision %s to %s" % (options.git_rev, connection)
        connection.sendall(tosend)

    def handle_need(self, connection, indata) :
        #TODO don't ignore 'indata' for solving CNF instances, use it to opitimize for uptime
        file_num = self.find_something_to_solve()

        #yay, everything finished!
        if file_num == None :
            tosend = {}
            tosend["noshutdown"] = options.noshutdown
            tosend["command"] = "finish"
            tosend = pickle.dumps(tosend)
            tosend = struct.pack('!q', len(tosend)) + tosend

            print "No more to solve, sending termination to ", connection
            connection.sendall(tosend)
        else :
            #set timer that we have sent this to be solved
            self.files_running[file_num] = time.time()
            filename = self.files[file_num].name

            tosend = {}
            tosend["file_num"] = file_num
            tosend["cnf_filename"] = filename
            tosend["solver"] = options.solver
            tosend["timeout_in_secs"] = options.timeout_in_secs
            tosend["mem_limit_in_mb"] = options.mem_limit_in_mb
            tosend["s3_bucket"] = options.s3_bucket
            tosend["s3_folder"] = options.s3_folder
            tosend["cnf_dir"] = options.cnf_dir
            tosend["noshutdown"] = options.noshutdown
            tosend["extra_opts"] = options.extra_opts
            tosend["uniq_cnt"] = str(self.uniq_cnt)
            tosend["command"] = "solve"
            tosend = pickle.dumps(tosend)
            tosend = struct.pack('!q', len(tosend)) + tosend

            print "Sending file %s (num %d) to %s" % (filename, file_num, connection)
            sys.stdout.flush()
            connection.sendall(tosend)
            self.uniq_cnt+=1

    def handle_one_connection(self):
        # Wait for a connection
        print >>sys.stderr, '--> waiting for a connection...\n\n'
        connection, client_address = self.sock.accept()

        try:
            print  time.strftime("%c"), 'connection from ', client_address

            data = get_n_bytes_from_connection(connection, 8)
            length = struct.unpack('!q', data)[0]
            data = get_n_bytes_from_connection(connection, length)
            data = pickle.loads(data)

            if data["command"] == "done" :
                self.handle_done(connection, data)

            elif data["command"] == "need" :
               self.handle_need(connection, data)

            elif data["command"] == "build" :
               self.handle_build(connection, data)

            sys.stdout.flush()
        except Exception as inst:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_exc()

            print "Exception type", type(inst), " dat: ", pprint.pprint(inst), " from ", client_address
            connection.close()
            return

        finally:
            # Clean up the connection
            print "Finished with client"
            connection.close()
            return

    def handle_all_connections(self):
        self.sock = self.listen_to_connection()
        while True:
            self.handle_one_connection()

server = Server()
server.handle_all_connections()

#c3.large
#def call() :
    #calling = """
    #aws ec2 request-spot-instances \
        #--dry-run \
        #--spot-price "0.025"
        #--instance-count 2
        #--type "one-time"
        #--launch-specification "{\"ImageId\":\"ami-AMI\",\"InstanceType\":"c3.large\",\"SubnetId\":\"subnet-SUBNET\", \"Monitoring\": {\"Enabled\": false},\"SecurityGroupIds\":\"launch-wizard-1\"}"
        #--image-id XXX \
        #--key-name mykey \
        #--security-groups "launch-wizard-1" \
        #--count XXX \
        #--monitoring Enabled=false
    #"""

exit(0)


import boto.ec2
conn = boto.ec2.connect_to_region("us-west-2")

ami_id = "ami-61f9a951"
vpc_id = "vpc-a19248c4"

reservations = conn.get_all_reservations()
num = 0
instances = []
for reservation in reservations:
    for instance in reservation.instances :
        if instance.instance_type != "t2.micro" :
            instances.append([instance.instance_type, instance.placement])

print "client instances running:", instances

requests = conn.get_all_spot_instance_requests()
print "Active requests:", requests
for req in requests:
    if ("%s" % req.status) != "<Status: instance-terminated-by-user>":
        print "-> ", [req.price, req.status]




