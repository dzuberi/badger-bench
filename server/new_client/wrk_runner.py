from abc import ABC, abstractmethod
import argparse
import os
import psutil
import re
import subprocess
import sys
import time
from io import StringIO
import pandas as pd
from datetime import datetime
from enum import Enum, auto

latency_percentiles = [50, 90, 99, 99.9, 99.99]
fields = ["preempt_type", "preempt_interval", "target", "achieved", "req_type", "duration", "preemptgen"]
next_port = 3000
numa_node = 1
data_path = "run.{}".format(datetime.now().strftime("%Y%m%d%H%M%S"))

lua_scripts = \
{ 
    "fakework" : {
        1: "uniform_1us.lua",
        10: "uniform_10us.lua",
        100: "uniform_100us.lua",
        260: "uniform_260us.lua",
        10000: "uniform_10ms.lua"
    },
    "badger" : {
        "short": "short.lua",
        "long": "long.lua",
    },
}

class PreemptType(Enum):
    SIGNAL = auto()
    UINTR = auto()

    def __str__(self):
        return self.name.lower()

class Server(ABC):
    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def stop(self):
        pass

    # subclasses should overwrite these methods in order to collect traces
    def start_trace(self):
        pass

    def stop_trace(self):
        pass

    # factory method for creating servers
    @staticmethod
    def create_server(exp):
        if exp["server_type"] == "fakework":
            return FakeWorkServer(exp)
        elif exp["server_type"] == "badger":
            return BadgerServer(exp)
        else:
            return ValueError(f"Invalid server type: {exp['server_type']}")

class BadgerServer(Server):
    def __init__(self, exp):
        self.exp = exp
        self.short_req = 'short.lua'
        self.short_name = 'short'
        self.long_req = 'long.lua'
        self.long_name = 'long'

    def start(self):
        global next_port

        self.exp['current_port'] = next_port
        next_port += 1

        env = f"GOMAXPROCS={self.exp['cores']} PREEMPT_INFO=1 GOFORCEPREEMPTNS={self.exp['preempt_interval']} "
        if self.exp["preempt_type"] == PreemptType.UINTR:
            env += "UINTR=1 "
        global data_path
        cmd = f"./server --keys_mil 250 --valsz 128 -port={self.exp['current_port']} -run={data_path}/{self.exp['name']} -dir=\"/data/db_data\""
        # cmd = f"numactl --cpunodebind {numa_node} "+cmd
        if self.exp['collect_traces']:
            cmd += " -trace"
        output = f" 2>&1 | ts %s > {server_output_file(self.exp)}"
        print(env + cmd + output)
        self.proc = subprocess.Popen(env + cmd + output, shell=True)
        self.start_time = time.time()

        # let the server start
        time.sleep(120)

        # fetch the PID for the server
        pids = get_pids("server")
        self.exp['server_pid'] = pids[0]

    def get_uptime(self):
        curr = time.time()
        return curr - self.start_time
        
    def stop(self):
        self.proc.terminate()
        self.proc.wait()
        del self.proc

        pids = get_pids("badger-server")
        for pid in pids:
            exec_cmd(f"sudo kill {pid}")

    def start_trace(self):
        exec_cmd(f"sudo kill -SIGUSR1 {self.exp['server_pid']}")

    def stop_trace(self):
        exec_cmd(f"sudo kill -SIGUSR2 {self.exp['server_pid']}")

class FakeWorkServer(Server):
    def __init__(self, exp):
        self.exp = exp
        self.short_req = 'uniform_1us.lua'
        self.short_name = '1us'
        self.long_req = 'uniform_260us.lua'
        self.long_name = '260us'

    def start(self):
        global next_port

        self.exp['current_port'] = next_port
        next_port += 1

        env = f"GOMAXPROCS={self.exp['cores']} PREEMPT_INFO=1 GOFORCEPREEMPTNS={self.exp['preempt_interval']} "
        if self.exp["preempt_type"] == PreemptType.UINTR:
            env += "UINTR=1 "
        global data_path
        cmd = f"numactl --cpunodebind {numa_node} ./fake-work-server -port={self.exp['current_port']} -run={data_path}/{self.exp['name']}"
        if self.exp['collect_traces']:
            cmd += " -trace"
        output = f" 2>&1 | ts %s > {server_output_file(self.exp)}"
        self.proc = subprocess.Popen(env + cmd + output, shell=True)

        # let the server start
        time.sleep(1)

        # fetch the PID for the server
        pids = get_pids("fake-work-server")
        self.exp['server_pid'] = pids[0]

    def stop(self):
        self.proc.terminate()
        self.proc.wait()
        del self.proc

        pids = get_pids("fake-work-server")
        for pid in pids:
            exec_cmd(f"sudo kill {pid}")

    def start_trace(self):
        exec_cmd(f"sudo kill -SIGUSR1 {self.exp['server_pid']}")

    def stop_trace(self):
        exec_cmd(f"sudo kill -SIGUSR2 {self.exp['server_pid']}")

def exec_cmd(cmd):
    result = subprocess.run(cmd, shell=True, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)

def new_experiment(cores, collect_traces, avg_service_time_us, bimodal,
                   preempt_type, preempt_interval, server_type):
    preempt_interval_str = str(int(preempt_interval / 1000)) + "us"

    if bimodal:
        lua_script = ""
    elif avg_service_time_us not in lua_scripts[server_type].keys():
        print(f"Unrecognized service time {avg_service_time_us}us")
        sys.exit()
    else:
        lua_script = lua_scripts[server_type][avg_service_time_us]

    exp = {
        'name': "{}-{}-{}".format(preempt_type, preempt_interval_str, "bimodal" if bimodal else lua_script.split('.')[0]),
        'cores': cores,
        'collect_traces': collect_traces,
        'avg_service_time_us': avg_service_time_us,
        'workload_file': lua_script,
        'bimodal': bimodal,
        'preempt_type': preempt_type,
        'preempt_interval': preempt_interval,
        'preempt_interval_str': preempt_interval_str,
        'server_type': server_type
        }

    global data_path
    os.makedirs(data_path + "/" + exp['name'])

    # copy some files into the results directory for the record
    with open(f"{data_path}/{exp['name']}/experiment.txt", "w") as f:
        f.write(str(exp) + "\n")
    if not bimodal:
        exec_cmd(f"cp {lua_script} {data_path}/{exp['name']}/")
    exec_cmd(f"cp wrk_runner.py {data_path}/{exp['name']}/")

    return exp

def results_file(exp):
    global data_path
    return f"{data_path}/{exp['name']}/results_{exp['preempt_interval_str']}.csv"

def server_output_file(exp):
    global data_path
    return f"{data_path}/{exp['name']}/server_{exp['current_port']}.out"

def summarize_wrk_results(target_rate, wrk):
    results_dict = {}
    results_dict["target"] = target_rate
    results_dict["req_type"] = wrk.req_type

    for l in wrk.stdout.split("\n"):
        pattern = r"(\d+\.\d+)%\s+(\d+\.\d+)(.?s)"
        match = re.search(pattern, l)
        if match:
            percentage = float(match.group(1))
            time_units = match.group(3)
            t = float(match.group(2))
            if time_units == "ms":
                t = t * 1000
            elif time_units == "s":
                t = t * 1000 * 1000
            elif time_units != "us":
                # unrecognized units, leave the whole string
                t = match.group(2)
            results_dict[percentage] = t

        pattern = r"Requests/sec:\s+(\d+\.\d+)"
        match = re.search(pattern, l)
        if match:
            results_dict["achieved"] = match.group(1)

    return results_dict

def summarize_wrk_results_caladan(target_rate, wrk):
    csv_lines = []
    for l in wrk.stdout.split("\n"):
        pattern = r"([^,]*,){6,}[^,]"
        match = re.search(pattern, l)
        if match:
            csv_lines.append(l.lower())
    csv_lines[0] = re.sub("actual","achieved",csv_lines[0])
    csv_lines[0] = re.sub("median","50",csv_lines[0])
    csv_lines[0] = re.sub(r'(\d+(\.\d+)?)th', r'\1', csv_lines[0])
    # csv_lines[0] = re.sub("([0-9].*)(\.[0.9].*)?th","\1\2",csv_lines[0])

    csv_data = StringIO("\n".join(csv_lines))
    df = pd.read_csv(csv_data)

    df["req_type"] = wrk.req_type
    df["start_time"] = wrk.start_time

    return df

def parse_server_output(output_file):
    results_dict = {}

    with open(output_file, "r") as f:
        for l in f:
            pattern = "executing for (\d+\.\d+) seconds"
            match = re.search(pattern, l)
            if match:
                results_dict["duration"] = float(match.group(1))

            pattern = r"total preemptgen: (\d+)"
            match = re.search(pattern, l)
            if match:
                results_dict["preemptgen"] = int(match.group(1))

    return results_dict

def get_pids(process_name):
    pids = []

    for proc in psutil.process_iter(['pid', 'name']):
        if process_name in proc.info['name']:
            pids.append(proc.info['pid'])

    return pids

# an instance of a wrk load-generating process
class Wrk:
    def __init__(self, command, request_type):
        self.cmd = command
        self.req_type = request_type

def benchmark_data_point(exp, target_rate):
    # start the server
    if exp['server_type'] != "badger":
        s = Server.create_server(exp)
        exp['server_obj'] = s
        s.start()
    s = exp['server_obj']

    try:
        s.start_trace()

        wrks = []

        if not exp['bimodal']:
            # command = f"wrk_ds -t{tc} -c{tc} -d50s -R{target_rate} -s {exp['workload_file']} --dist exp --latency http://127.0.0.1:{exp['current_port']}"
            if exp['workload_file'] == "short.lua":
                uri= "/getkey/1"
            else:
                uri = "/iteratekey/800/1"

            command = f"ssh hp019.utah.cloudlab.us \"/data/caladan/apps/synthetic/target/release/synthetic 10.10.1.2:{exp['current_port']} --threads 100 --protocol http --transport tcp --http_uri {uri} --runtime 20 --mode runtime-client --mpps {target_rate/1e6} --config /data/caladan/client-go.config\""
            # full_command = f"numactl --cpunodebind {numa_node} " + command
            full_command = command

            wrk = Wrk(full_command, str(exp['avg_service_time_us']))
            wrks.append(wrk)

            print(f"Running benchmark with command {full_command}")

            wrk.proc = subprocess.Popen(full_command, shell=True, text=True,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
            wrk.start_time = s.get_uptime()
        else:
            # assume 95% short, 5% long
            # 12 wrk threads and conns for short, 1 thread and conn for long

            short_rate = int(target_rate * 0.95)
            # command_short = f"wrk_ds -t50 -c50 -d50s -R{short_rate} -s {s.short_req} --dist exp --latency http://127.0.0.1:{exp['current_port']}"
            # full_command_short = f"numactl --cpunodebind {numa_node} " + command_short
            full_command_short = f"ssh hp019.utah.cloudlab.us \"/data/caladan/apps/synthetic/target/release/synthetic 10.10.1.2:{exp['current_port']} --threads 800 --protocol http --transport tcp --http_uri /getkey/1 --runtime 20 --mode runtime-client --mpps {short_rate/1e6} --config /data/caladan/client1.config\""
            wrk = Wrk(full_command_short, s.short_name)
            wrks.append(wrk)

            print(f"Running wrk for short requests with command {full_command_short}")
            wrk.proc = subprocess.Popen(full_command_short, shell=True, text=True,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
            wrk.start_time = s.get_uptime()

            long_rate = int(target_rate * 0.05)
            # command_long = f"wrk_ds -t10 -c10 -d50s -R{long_rate} -s {s.long_req} --dist exp --latency http://127.0.0.1:{exp['current_port']}"
            # full_command_long = f"numactl --cpunodebind {numa_node} " + command_long
            full_command_long = f"ssh hp019.utah.cloudlab.us \"/data/caladan/apps/synthetic/target/release/synthetic 10.10.1.2:{exp['current_port']} --threads 100 --protocol http --transport tcp --http_uri /iteratekey/800/1 --runtime 20 --mode runtime-client --mpps {long_rate/1e6} --config /data/caladan/client2.config\""
            wrk = Wrk(full_command_long, s.long_name)
            wrks.append(wrk)

            print(f"Running wrk for long requests with command {full_command_long}")
            wrk.proc = subprocess.Popen(full_command_long, shell=True, text=True,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
            wrk.start_time = s.get_uptime()

        for wrk in wrks:
            wrk.stdout, wrk.stderr = wrk.proc.communicate()
        s.stop_trace()

        # stop the server
        if exp['server_type'] != "badger":
            s.stop()

        global data_path
        # with open(results_file(exp), "w") as f_out:
        all_wrk_results = []
        for wrk in wrks:
            with open(f"{data_path}/{exp['name']}/output_{target_rate}_{wrk.req_type}.stdout", "w") as f:
                f.write(wrk.stdout)
            with open(f"{data_path}/{exp['name']}/output_{target_rate}_{wrk.req_type}.stderr", "w") as f:
                f.write(wrk.stderr)

            # aggregate results
            all_results = exp.copy()

            # parse the output from the server
            server_results = parse_server_output(server_output_file(exp))
            all_results.update(server_results)

            # parse and summarize the results
            wrk_results = summarize_wrk_results_caladan(target_rate, wrk)
            print(wrk_results)
            #all_results.update(wrk_results)
            
            for k,v in all_results.items():
                if k == "server_obj":
                    continue
                wrk_results[k] = v

            # write results to file
            #all_output_fields = fields + latency_percentiles
            #f_out.write(",".join([str(all_results.get(x)) for x in all_output_fields]) + "\n")
            all_wrk_results.append(wrk_results)
        df = pd.concat(all_wrk_results, ignore_index=True)
        df.to_csv(results_file(exp))

    except subprocess.CalledProcessError as e:
            print(f"Benchmark failed with return code {e.returncode}")

# compute offered loads for a given experiment
def offered_loads(exp):
    min_load = 0

    if exp['cores'] == 1:
        # config for 1 proc
        if exp['bimodal']:
            max_load = 60 * 1000
        else:
            max_load = 400 * 1000
            
        if exp['server_type'] == "badger":
            if exp['avg_service_time_us'] == "short":
                max_load = 20000
            elif exp['avg_service_time_us'] == "long":
                max_load = 8000
            
            if exp['bimodal']:
                max_load = 20 * 1000

        num_steps = 10
        step_size = int((max_load - min_load) / num_steps)
    else:
        print(f"warning: rates may not be optimal for {exp['cores']} cores")
        max_load = 40 * 1000 * exp['cores']
        num_steps = 8
        step_size = int((max_load - min_load) / num_steps)

    return [min_load + step_size * (i + 1) for i in range(num_steps)]

def benchmark_system(cores, collect_traces, avg_service_time_us, bimodal, preempt_type,
                     preempt_interval, server_type):
    print(f"Benchmarking with preemption {preempt_type} and interval {preempt_interval}ns")

    # create the experiment and a directory for it
    if not isinstance(avg_service_time_us, list):
        avg_service_time_us = [avg_service_time_us]
    
    if bimodal:
        avg_service_time_us = [""]

    for service_time in avg_service_time_us:
        exp = new_experiment(cores, collect_traces, service_time, bimodal,
                             preempt_type, preempt_interval, server_type)

        with open(results_file(exp), "w") as f_out:
            hdr_str = ",".join(fields + ["p" + str(x) for x in latency_percentiles])
            f_out.write(f"{hdr_str}\n")

        # run the wrk client at several different loads
        
        # avoid starting the server repeatedly in the case of badger, since it takes a while to spin up
        if server_type == "badger":
            s = Server.create_server(exp)
            exp['server_obj'] = s
            s.start()
        # for rate in offered_loads(exp):
            # benchmark_data_point(exp, rate)
        benchmark_data_point(exp, offered_loads(exp)[-1])

            # sleep briefly between data points
        time.sleep(1)

        # generate PNGs for all CPU profiles
        global data_path
        for filename in os.listdir(f"{data_path}/{exp['name']}"):
            if filename.endswith(".prof"):
                png_filename = filename.replace(".prof", ".png")
                exec_cmd(f"go tool pprof -png -output {data_path}/{exp['name']}/{png_filename} {data_path}/{exp['name']}/{filename}")
    if server_type == "badger":
        s.stop()

def main():
    exec_cmd("sudo echo") #prompt password so you don't have to wait
    parser = argparse.ArgumentParser(description='run basic go server experiments')

    parser.add_argument('-c', '--cores', type=int, default=1, help='number of cores')
    parser.add_argument('-uintr', action="store_true", help='run with user interrupts')
    parser.add_argument('-signal', action="store_true", help='run with signals')
    parser.add_argument('-trace', action="store_true", help='run with traces')
    parser.add_argument('-bimodal', action="store_true", help='use a bimodal distribution')
    parser.add_argument('-server', type=str, default='fakework', help='type of server to benchmark')
    args = parser.parse_args()

    start_time = datetime.now()

    if args.server == "fakework":
        avg_service_time_us = 1
    else:
        avg_service_time_us = ["short", "long"]
    target_intervals = [10 * 1000 * 1000, 100 * 1000, 10 * 1000]
    for interval in target_intervals:
        if args.uintr:
            benchmark_system(args.cores, args.trace, avg_service_time_us, args.bimodal,
                             PreemptType.UINTR, interval, args.server)
        if args.signal:
            benchmark_system(args.cores, args.trace, avg_service_time_us, args.bimodal,
                             PreemptType.SIGNAL, interval, args.server)

    duration = datetime.now() - start_time
    print(f"Total experiment runtime: {duration.total_seconds()} seconds")

if __name__ == "__main__":
    main()

