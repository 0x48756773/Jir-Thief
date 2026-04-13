<h1 align="center">
  <img src="static/jir_thief-banner.png" alt="jir-thief" width="400px"></a>
  <br>
</h1>
<p align="center">
<a href="https://twitter.com/antman1p"><img src="https://img.shields.io/twitter/follow/antman1p.svg?logo=twitter"></a>
</p>

# Jir-Thief
Jir-Thief connects to Jira's API using an access token, searches issues using a keyword dictionary, and downloads matching issues as Word `.doc` files to the `./loot` directory. Designed for authorised red-team and penetration testing engagements.

## Dependencies
```
pip install requests
```

## Warning!!!
Using a large keyword list will produce a significant number of results, send a large volume of API requests, and take considerable time to complete. Start with a smaller or more targeted dictionary if you want to limit scope.

## Usage
```
python3 jir_thief.py [-h] -j <TARGET URL> -u <USERNAME> -p <API TOKEN> -d <DICTIONARY> [-a "<UA STRING>"] [-t <THREADS>] [-s <SEARCH THREADS>]
```

### Arguments

| Flag | Long form | Description | Required |
|------|-----------|-------------|----------|
| `-j` | `--url` | Target Jira URL | Yes |
| `-u` | `--user` | Target Jira account username | Yes |
| `-p` | `--accesstoken` | API access token | Yes |
| `-d` | `--dict` | Path to keyword dictionary file | Yes |
| `-a` | `--user-agent` | Custom User-Agent string (default: `python-requests/2.25.1`) | No |
| `-t` | `--threads` | Concurrent download threads (default: `5`) | No |
| `-s` | `--search-threads` | Concurrent keyword search threads (default: `3`) | No |
| `-h` | `--help` | Show help and exit | No |

### Example
```
python3 jir_thief.py \
  -j https://target.atlassian.net \
  -u user@target.com \
  -p <API_TOKEN> \
  -d ./dictionaries/pii.txt \
  -t 10 \
  -s 5
```

## Features

### Threaded Search
Keywords are searched concurrently across multiple threads (`-s`, default 3). Each thread independently paginates through all results for its keyword using Jira's API v3 cursor-based pagination. Increasing `-s` reduces total search time proportionally.

### Threaded Downloads
Files are downloaded concurrently using a thread pool (`-t`, default 5). Concurrency is controlled by a semaphore independent of the thread pool, allowing live adjustment without rebuilding the executor.

### Adaptive Rate Limiting
Both the search and download phases detect HTTP `429 Too Many Requests` responses and react automatically:
- The `Retry-After` header is respected if present
- On a 429, one download thread is removed from the active pool (a semaphore slot is stolen)
- After 60 seconds of no rate limiting, a thread slot is restored
- 429 responses do not count against the per-file error retry limit

### Resume — Search
After each keyword completes, progress is saved to `search_resume.json` containing all finished terms and collected issue keys. On restart, completed terms are skipped and previously found keys are restored automatically. The file is deleted when all terms finish cleanly.

```
[*] Resume file found: 89 terms already completed, 14302 keys loaded
[*] Skipping 89 already-searched term(s)
[*] Searching 51 term(s) with 3 thread(s)
```

### Resume — Downloads
Any `.doc` file already present in `./loot` is skipped automatically. Re-running the tool after an interruption continues from where it left off with no manual intervention.

### Error Handling
- Per-file retry with exponential backoff (up to 3 attempts)
- Failed downloads are collected and reported in a summary at the end
- Empty or malformed API responses are caught and logged without crashing
- Worker thread exceptions are caught at the future level so one bad term or file does not abort the run

### Progress & ETA
Download progress is printed every 30 seconds showing overall ETA for the entire job:
```
[*] Progress: 1200/77000 (1.6%) | Threads: 5 | Elapsed: 00:06:00 | ETA: 06:20:00 | Failed: 2
```

## Dictionaries

| File | Description |
|------|-------------|
| `dictionaries/secrets-keywords.txt` | API keys, tokens, credentials, and secrets |
| `dictionaries/pii.txt` | Personally identifiable information (PII/PHI) |

### pii.txt coverage
- Personal identifiers — SSN, DOB, full name, maiden name
- Government IDs — passport, driver's licence, TIN, EIN, ITIN, visa
- **Canadian PII** — SIN, provincial health cards (OHIP, MSP, AHCIP, etc.), CRA tax forms (T4, T4A, T1, NOA), RRSP/TFSA/RESP, transit/institution numbers, Interac, PIPEDA, CASL
- Financial — credit/debit card, CVV, IBAN, SWIFT, ACH, routing number, payroll, W-2
- Health / PHI — MRN, patient data, diagnosis, prescription, HIPAA, Medicare, biometrics
- HR / Employment — employee ID, background check, I-9, W-4, direct deposit, FMLA
- Legal / Regulatory — GDPR, CCPA, data subject requests, right to erasure

### Comment support
Dictionary files support `#` comment lines and blank lines — both are ignored during search.

## Proxy Support (Burp Suite etc.)
Set the following environment variables to route traffic through a proxy:
```bash
export REQUESTS_CA_BUNDLE='/path/to/cert.pem'
export HTTP_PROXY="http://127.0.0.1:8080"
export HTTPS_PROXY="http://127.0.0.1:8080"
```

## TODO
- Logging to file
- Map keyword searches to downloaded files
