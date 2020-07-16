from urllib2 import urlopen, Request

def get_cloudflare_ips(version='v4'):
    url = "https://www.cloudflare.com/ips-{}".format(version)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/41.0.2228.0 Safari/537.3"}
    resp = urlopen(
        Request(url=url, headers=headers)
    )
    return resp.read().decode('utf-8').strip().split('\n')
