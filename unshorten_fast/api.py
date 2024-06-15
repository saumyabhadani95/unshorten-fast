"""
Expand URLs from shortening services

This script takes a list of shortened URLs and expands them to their original form.
It can handle a large number of URLs concurrently using asynchronous programming with aiohttp.

Usage:
    python api.py input.txt output.txt [options]

Options:
    -m, --maxlen LEN           Ignore domains longer than LEN
    -d, --domains PATH         Expand if domain is present in CSV file at PATH
    --domains-noheader         CSV file with domains has no header
    --no-cache                 Disable cache
    --debug                    Enable debug logging
"""

import aiohttp
import argparse
import asyncio
import time
import logging
from urllib.parse import urlsplit
import re

import os
from dotenv import load_dotenv
import redis

# Access environment variables
load_dotenv()
redis_host = os.getenv('REDIS_HOST')
redis_port = os.getenv('REDIS_PORT')
redis_password = os.getenv('REDIS_PASSWORD')

TTL_DNS_CACHE = 300  # Time-to-live of DNS cache
MAX_TCP_CONN = 50  # Throttle at max these many simultaneous connections
TIMEOUT_TOTAL = 100  # Each request times out after these many seconds

LOG_FMT = "%(asctime)s:%(levelname)s:%(message)s"
logging.basicConfig(format=LOG_FMT, level="INFO")
_STATS = {
    "ignored": 0,
    "timeout": 0,
    "error": 0,
    "cached": 0,
    "cached_retrieved": 0,
    "expanded": 0
}


def make_parser():
    """Create a command-line argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("-m",
                        "--maxlen",
                        type=int,
                        metavar="LEN",
                        help="Ignore domains longer than %(metavar)s")
    parser.add_argument("-d",
                        "--domains",
                        dest="domains_path",
                        metavar="PATH",
                        help="Expand if domain is present in CSV file at %(metavar)s")
    parser.add_argument("--domains-noheader",
                        action="store_false",
                        dest="skip_header",
                        help="CSV file with domains has no header")
    parser.add_argument("--no-cache",
                        action="store_true",
                        help="disable cache")
    parser.add_argument("--debug",
                        action="store_const",
                        const="DEBUG",
                        dest="log_level")
    parser.set_defaults(log_level="INFO")
    return parser


async def unshortenone(url, session, pattern=None, maxlen=None,
                       cache=None, timeout=None):
    """
    Expand a single shortened URL.

    Args:
        url (str): The shortened URL to expand.
        session (aiohttp.ClientSession): The aiohttp session for making HTTP requests.
        pattern (re.Pattern, optional): Regex pattern to match specific domains.
        maxlen (int, optional): Maximum length for URLs.
        cache (dict, optional): Cache to store expanded URLs.
        timeout (aiohttp.ClientTimeout, optional): Timeout for HTTP requests.

    Returns:
        str: The expanded URL or the original URL if expansion failed.
    """
    # If user specified list of domains, check netloc is in it, otherwise set
    # to False (equivalent of saying there is always a match against the empty list)
    if pattern is not None:
        domain = urlsplit(url).netloc
        match = re.search(pattern, domain)
        no_match = (match is None)
    else:
        no_match = False
    # If user specified max URL length, check length, otherwise set to False
    # (equivalent to setting max length to infinity -- any length is OK)
    too_long = (maxlen is not None and len(url) > maxlen)
    # Ignore if either of the two exclusion criteria applies.
    if too_long or no_match:
        _STATS["ignored"] += 1
        return url
    cached_ans = None if cache is None else cache.get(url)
    if cached_ans is not None:
        _STATS["cached_retrieved"] += 1
        return cached_ans.decode("utf-8")
    else:
        try:
            # await asyncio.sleep(0.01)
            resp = await session.head(url, timeout=timeout,
                                      ssl=False, allow_redirects=True)
            expanded_url = str(resp.url)
            if url != expanded_url:
                _STATS['expanded'] += 1
                # if cache is not None and url not in cache:
                if cache is not None:
                    cache.set(url, expanded_url)
                    # update cache if needed
                    _STATS["cached"] += 1
                    # cache[url] = expanded_url
            return expanded_url
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as e:
            _STATS["error"] += 1
            if isinstance(e, asyncio.TimeoutError):
                _STATS["timeout"] += 1
            logging.debug(f"{e.__class__.__name__}: {e}: {url}")
            return url


# Thanks: https://blog.jonlu.ca/posts/async-python-http
async def gather_with_concurrency(n, *tasks):
    """
    Run multiple asynchronous tasks with a concurrency limit.

    Args:
        n (int): Maximum number of concurrent tasks.
        tasks (list): The tasks to run.

    Returns:
        list: The results of the tasks.
    """
    semaphore = asyncio.Semaphore(n)

    async def sem_task(task):
        async with semaphore:
            return await task
    return await asyncio.gather(*(sem_task(task) for task in tasks))


async def _unshorten(*urls, cache=None, domains=None, maxlen=None):
    """
    Expand a list of shortened URLs.

    Args:
        urls (list): The list of URLs to expand.
        cache (dict, optional): Cache for expanded URLs.
        domains (list, optional): List of domains to match.
        maxlen (int, optional): Maximum length for URLs.

    Returns:
        list: The expanded URLs.
    """
    if domains is not None:
        pattern = re.compile(f"({'|'.join(domains)})", re.I)
    else:
        pattern = None
    conn = aiohttp.TCPConnector(ttl_dns_cache=TTL_DNS_CACHE, limit=None)
    u1 = unshortenone
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_TOTAL)
    async with aiohttp.ClientSession(connector=conn) as session:
        return await gather_with_concurrency(MAX_TCP_CONN,
                                             *(u1(u, session, cache=cache,
                                                  maxlen=maxlen,
                                                  pattern=pattern,
                                                  timeout=timeout) for u in urls))


def unshorten(*args, **kwargs):
    """
    Wrapper function to run _unshorten synchronously.

    Args:
        *args: Arguments for _unshorten.
        **kwargs: Keyword arguments for _unshorten.

    Returns:
        list: The expanded URLs.
    """
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(_unshorten(*args, **kwargs))


def _main(args):
    """
    Main function to process input and output files.

    Args:
        args (argparse.Namespace): Command-line arguments.
    """
    try:
        logging.basicConfig(level=args.log_level, format=LOG_FMT, force=True)
        logging.info(args)
        if args.domains_path is not None:
            with open(args.domains_path) as f:
                if args.skip_header:
                    f.readline()
                domains = [line.strip(',\n') for line in f]
        else:
            domains = None
        if args.no_cache:
            cache = None
        else:
            # cache = {}
            # cache = redis.Redis(
            #     host=redis_host,
            #     port=redis_port,
            #     password=redis_password,
            #     ssl=True
            # )
            cache = redis.Redis(host='localhost', port=6379)
        tic = time.time()
        with open(args.input, encoding="utf8") as inputf:
            shorturls = (url.strip(" \n") for url in inputf)
            urls = unshorten(*shorturls, cache=cache, domains=domains,
                             maxlen=args.maxlen)
        with open(args.output, "w", encoding="utf8") as outf:
            outf.writelines((u + "\n" for u in urls))
        toc = time.time()
        elapsed = toc - tic
        rate = len(urls) / elapsed
        logging.info(f"Processed {len(urls)} urls in {elapsed:.2f}s ({rate:.2f} urls/s))")
        logging.info(f"Processed {len(urls)} urls in {elapsed:.2f}s ({rate:.2f} urls/s))")

    except KeyboardInterrupt:
        import sys
        print(file=sys.stderr)
        logging.info("Interrupted by user.")
    finally:
        logging.info(f"Ignored: {_STATS['ignored']:.0f}")
        logging.info(f"Expanded: {_STATS['expanded']:.0f}")
        logging.info(f"Cached: {_STATS['cached']:.0f} ({_STATS['cached_retrieved']:.0f} hits)")
        logging.info(f"Errors: {_STATS['error']:.0f} ({_STATS['timeout']:.0f} timed out)")


def main():
    """Entry point for the script."""
    parser = make_parser()
    args = parser.parse_args()
    _main(args)


if __name__ == "__main__":
    main()
