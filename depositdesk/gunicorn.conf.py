"""Sandbox deployment behind a buffering reverse proxy."""
import os
bind = os.environ.get('DEPOSITDESK_BIND', '127.0.0.1:8765')
workers = 2
worker_class = 'gthread'
threads = 4
timeout = 90
graceful_timeout = 90
keepalive = 2
umask = 0o077
accesslog = '-'
errorlog = '-'
# No query strings, cookies, authorization headers or request bodies in access logs.
access_log_format = '%(t)s %(m)s %(U)s %(s)s %(L)s'
limit_request_line = 2048
limit_request_fields = 40
limit_request_field_size = 4096
forwarded_allow_ips = '127.0.0.1,::1'
