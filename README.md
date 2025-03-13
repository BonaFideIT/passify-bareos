# passify-bareos

A script for transforming Bacula / Bareos job results into an Icinga2 API passive check result. This is designed to be used with a `RunScript` after the job has completed on the agent.

## Installation

1. Clone this repository to your server.
2. Create a configuration file or run the script interactively for the first time to provide the required information:
   - **Icinga Master API URL** (submissions are only possible through the currently active master).
   - **Verify the fingerprint**:
     ```sh
     openssl x509 -in /var/lib/icinga2/certs/<hostname>.crt -noout -fingerprint -sha256
     ```
   - **Username and password** for API submission (only basic-auth is currently supported).

Alternatively, deploy a configuration file (`config.ini` by default) alongside the script:

```ini
[DEFAULT]
url = https://localhost:5665/v1/actions/process-check-result
check_source = example.com
user = <api_user>
password = <api_password>

[TLS]
fingerprint = d163f22c2021a498926ff8c30da0288ac20d1b9edaa80d1dbb14c0aebf85245b
```

## Creating an API User for Passive Result Submission

Add the following configuration to `/etc/icinga2/features-available/api.conf` on the master:

```icinga2
object ApiUser "<api_user>" {
  permissions = [ "actions/process-check-result" ]
  password = "<api_password>"
}
```

Ensure that the API feature in Icinga2 is activated.

## Deployment

### Usage

```sh
usage: passify-bareos.py [-h] [--config CONFIG] [--timeout TIMEOUT] [--ttl TTL] -s SERVICE_NAME [--prefix-time]
                         {Backup} {Full,Differential,Incremental,VirtualFull} {OK,Error,Fatal Error,Canceled,Differences,Unknown term code} JOB_UID size
```

#### Positional Arguments:
- `{Backup}`: Bareos job type (currently only `Backup` is supported).
- `{Full,Differential,Incremental,VirtualFull}`: Backup job level.
- `{OK,Error,Fatal Error,Canceled,Differences,Unknown term code}`: Backup job result value.
- `JOB_UID`: Unique job name (e.g., `jobname.date.time...`).
- `size`: Number of processed bytes for the backup job.

#### Options:
- `-h, --help`: Show help message and exit.
- `--config CONFIG`: Path to configuration file (default: `config.ini`).
- `--timeout TIMEOUT`: Optional execution timeout in seconds.
- `--ttl TTL`: Time-to-live argument for Icinga API.
- `-s SERVICE_NAME`: Specify the service name.
- `--prefix-time`: Prefix the service name with the scheduled time.

### Integration into Bacula / Bareos

To integrate, create a `RunScript` directive inside your job definition and add a passive check to your Icinga instance.

#### Job Definition

Add the following `RunScript` directive inside your job configuration:

```bareos
RunScript {
    RunsWhen = After
    RunsOnSuccess = Yes
    RunsOnFailure = Yes
    RunsOnClient = Yes
    FailJobOnError = No
    Command = "/bin/bash -c '/usr/local/bin/passify-bareos/src/passify-bareos/passify-bareos.py --prefix-time -s \"backup check\" \"%t\" \"%l\" \"%e\" \"%j\" \"%b\"'"
}
```

Modify the `Command` as needed, but note that the interface is designed to take these parameters in this sequence.

#### Icinga Passive Check

Add a passive check for the host where the agent/filedaemon runs and should be backed up.

Example Icinga Director configuration:

```icinga2
object Service "23:00:00 backup check" {
    host_name = "example.com"
    check_command = "passive-crit"
    max_check_attempts = "1"
    check_interval = 1d
    retry_interval = 15s
    enable_active_checks = true
    enable_passive_checks = true
    enable_perfdata = true
    volatile = false
    command_endpoint = host_name
    vars.dummy_state = 2
}
```

Setting `dummy_state = 2` marks a missing passive check as **critical**, as it likely indicates that a backup was not completed at all.
Modify the check interval to the shortest expected backup completion window.

### Monitoring Multiple Backup Jobs per Host in 24h

To monitor multiple backup jobs per day:

- Create one check per backup time window.
- Prefix the service name with the scheduled backup time (e.g., `23:00:00`).
- Use the `--prefix-time` option to dynamically match the expected backup time.

---

This improved documentation enhances readability, formatting, and clarity while ensuring completeness and correctness.
