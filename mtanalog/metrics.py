"""Bounded anonymous request statistics; never stores addresses, URLs or cookies."""
from collections import deque
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import threading
import time

from .access import private_write


class LoadMetrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.salt = secrets.token_bytes(32)
        self.clients = {}
        self.rows = deque(maxlen=1440)
        self.minute = None
        self.current = {}
        self.durations = deque(maxlen=1024)
        self.cpu_sample = (time.monotonic(), time.process_time())

    def observe(self, status, elapsed_ms, size, client=None, rejected=False, now=None):
        now = time.time() if now is None else now
        minute = int(now//60)*60
        with self.lock:
            if self.minute != minute:
                if self.current:
                    self.rows.append(self._row())
                self.minute = minute
                self.current = dict(minute=minute, requests=0, errors=0, rejected=0, bytes=0)
                self.durations.clear()
            self.current['requests'] += 1
            self.current['errors'] += int(status >= 500 or status == 429)
            self.current['rejected'] += int(rejected)
            self.current['bytes'] += size
            self.durations.append(elapsed_ms)
            self.clients = {k:v for k,v in self.clients.items() if now-v<300}
            if client and len(client)==36 and all(c in '0123456789abcdef-' for c in client):
                key = hashlib.sha256(self.salt+client.encode()).hexdigest()
                if len(self.clients)<2000 or key in self.clients:
                    self.clients[key] = now

    def _row(self):
        values = sorted(self.durations)
        return {**self.current, 'p95_ms': round(values[max(0,math.ceil(len(values)*.95)-1)],1) if values else 0}

    def snapshot(self):
        now = time.time()
        with self.lock:
            rows = [r for r in self.rows if now-r['minute']<86400]
            if self.current and now-self.minute<86400:
                rows.append(self._row())
            active = sum(now-v<300 for v in self.clients.values())
            monotonic,cpu=time.monotonic(),time.process_time()
            previous,used=self.cpu_sample
            cpu_pct=round(100*(cpu-used)/max(.001,monotonic-previous),1)
            self.cpu_sample=(monotonic,cpu)
        mem = {}
        try:
            for line in Path('/proc/meminfo').read_text().splitlines():
                key,value,*_=line.split()
                if key in ('MemTotal:','MemAvailable:'):mem[key[:-1]]=int(value)*1024
        except OSError:
            pass
        recent=[r for r in rows if now-r['minute']<300]
        return dict(updated_ms=int(now*1000),active_visitors_5m=active,
                    requests_5m=sum(r['requests'] for r in recent),
                    errors_5m=sum(r['errors'] for r in recent),
                    rejected_5m=sum(r['rejected'] for r in recent),
                    response_p95_ms=max((r['p95_ms'] for r in recent),default=0),
                    web_cpu_pct=cpu_pct,load_average=list(os.getloadavg()),
                    cpu_count=os.cpu_count(),memory=mem,history=rows)

    def persist(self, root, stopping, readiness):
        while not stopping.wait(30):
            try:
                value=self.snapshot();value['collector']=readiness()[1]
                private_write(Path(root)/'web-metrics.json',json.dumps(value))
            except Exception:
                # Monitoring failure must not stop data collection or HTTP service.
                continue
