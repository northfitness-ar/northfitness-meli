"""Bounded, credential-free process diagnostics for the public health check."""
import asyncio
import os
import resource
import time


def resident_bytes():
    """Return current RSS on Linux; fall back to the process high-water mark."""
    try:
        with open('/proc/self/statm', encoding='ascii') as statm:
            pages = int(statm.read().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        high_water = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(high_water * (1024 if os.name == 'posix' else 1))


def open_file_descriptors():
    try:
        with os.scandir('/proc/self/fd') as entries:
            return sum(1 for _ in entries)
    except OSError:
        return None


class RuntimeDiagnostics:
    """Small counters only: no URLs, headers, tokens, payloads, or task names."""
    def __init__(self):
        self.started_at = time.time()
        self.initial_rss = resident_bytes()
        self.meli_requests = 0
        self.meli_failures = 0
        self.meli_active = 0
        self.meli_peak_active = 0

    def request_started(self):
        self.meli_requests += 1
        self.meli_active += 1
        self.meli_peak_active = max(self.meli_peak_active, self.meli_active)

    def request_finished(self, failed=False):
        self.meli_active -= 1
        if failed:
            self.meli_failures += 1

    def snapshot(self):
        rss = resident_bytes()
        try:
            tasks = asyncio.all_tasks()
        except RuntimeError:
            tasks = ()
        return {
            'uptime_seconds': round(time.time() - self.started_at, 1),
            'rss_bytes': rss,
            'open_file_descriptors': open_file_descriptors(),
            'rss_change_bytes': rss - self.initial_rss,
            'asyncio_tasks': len(tasks),
            'asyncio_pending_tasks': sum(not task.done() for task in tasks),
            'meli_requests_total': self.meli_requests,
            'meli_requests_failed': self.meli_failures,
            'meli_requests_active': self.meli_active,
            'meli_requests_peak_active': self.meli_peak_active,
        }

