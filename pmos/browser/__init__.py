"""Browser boundary: attach to the human-logged-in Chrome, send requests through it.

The collector never launches, closes, navigates or logs in the browser. The
browser runs as its own service with a loopback DevTools port, so restarting
the collector never loses the website login.
"""
