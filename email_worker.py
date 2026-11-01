"""Dedicated PESO-IMS RQ worker: python email_worker.py"""
import os
from redis import Redis
from rq import Queue, Worker


def main():
    url = os.environ.get('PESO_EMAIL_QUEUE_URL', '').strip()
    if not url:
        raise RuntimeError('PESO_EMAIL_QUEUE_URL is required to run the email worker')
    connection = Redis.from_url(url)
    connection.ping()
    queue = Queue('peso-emails', connection=connection)
    Worker([queue], connection=connection).work()


if __name__ == '__main__':
    main()
