import multiprocessing
import os

# Server socket
bind = "0.0.0.0:8000"
backlog = 2048

# Worker processes
# Calculate workers based on cores, with a sane minimum and maximum
cores = multiprocessing.cpu_count()
workers = min(int(os.getenv("MAX_WORKERS", 4)), max(2, cores * 2 + 1))
worker_class = "uvicorn_worker.UvicornWorker"
worker_connections = 1000
timeout = 30
keepalive = 2

# Global Rate Limiting / Nginx Edge integration
# Uvicorn reads the X-Forwarded-For header to determine the true client IP
# ONLY trust our Nginx reverse proxy, but in Docker Compose we use '*'
# because the internal Docker IP changes, and Nginx edge strips malicious headers.
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "*")

# Logging
# We use custom JSON logging in the application, so we pipe Gunicorn's logs to it
accesslog = "-"
errorlog = "-"
loglevel = "info"
