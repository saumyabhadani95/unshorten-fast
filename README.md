# unshorten-fast

Expand URLs from shortening services (e.g. bit.ly) _fast_. Uses
[aiohttp](https://docs.aiohttp.org/) to run multiple web requests
concurrently.

## Features

- Single-threaded, asynchronous I/O for low CPU footprint;
- Uses HTTP's HEAD command instead of GET to reduce bandwidth;
- Internal caching to reduce amount of requests sent;
- Flexible inclusion criteria:
  + User-defined domain inclusion list;
  - URL length;
