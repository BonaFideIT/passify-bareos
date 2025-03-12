# passify-bareos

A script for transforming a bacula / bareos job results into an icinga2 api passive check result. This is designed to be used with runScript after the job has completed on the agent.

# Installation

* clone this repository to your server
* create config file or just run it and the first time you will be asked all required information
  * the icinga master api url **submissions are only possible through the currently active master**
  * verify the fingerprint(hint: openssl x509 -in /var/lib/icinga2/certs/<hostname>.crt -noout -fingerprint -sha256)
username and password for api submission (only basic-auth is currently supported)

**OR**

deploy a confign file along with the file, default is config.ini:

```
[DEFAULT]
url = https://localhost:5665/v1/actions/process-check-result
check_source = example.com
user = <api_user>
password = <api_password>

[TLS]
fingerprint = d163f22c2021a498926ff8c30da0288ac20d1b9edaa80d1dbb14c0aebf85245b
```

# howto: create api user for passive result submission

add this to /etc/icinga2/features-available/api.conf on the master:

```
object ApiUser "<api_user>" {
  permissions = [ "actions/process-check-result" ]
  password = "<api_password>"
}
```

And please dont forget to activate the api feature in icinga2.

# Deployment

## usage

usage: passify-bareos.py [-h] [--config CONFIG] [--timeout TIMEOUT] [--ttl TTL] -s SERVICE NAME [--prefix-time]
                         {Backup} {Full,Differential,Incremental,VirtualFull} {OK,Error,Fatal Error,Canceled,Differences,Unknown term code} JOB_UID size

positional arguments:
  {Backup}              Bareos job type. Currently only Backup supported.
  {Full,Differential,Incremental,VirtualFull}
                        Backup job level.
  {OK,Error,Fatal Error,Canceled,Differences,Unknown term code}
                        Backup job result value.
  JOB_UID               job unique name: e.g. jobname.date.time...
  size                  Number of pocessed bytes for the backup job.

options:
  -h, --help            show this help message and exit
  --config CONFIG       Path where to store/load config from. [default=config.ini]
  --timeout TIMEOUT     Optional timeout for execution in seconds.
  --ttl TTL             TTL argument to pass to icinga api
  -s SERVICE NAME       Specify service name
  --prefix-time         Enable prefixing the service name with the scheduled time.

## integration into bacula / bareos

You need to create a runScript directive inside your job definition and add a passive check to your icinga instance. 

### job definition

Create a runScript directive inside your job definition like this:

```
RunScript {
        RunsWhen = After
        RunsOnSuccess = Yes
        RunsOnFailure = Yes
        RunsOnClient = Yes
        FailJobOnError = No
        Command = "/bin/bash -c '/usr/local/bin/passify-bareos/src/passify-bareos/passify-bareos.py --prefix-time -s "backup check" \"%t\" \"%l\" \"%e\" \"%j\" \"%b\" > /tmp/test.txt'"
    }
```

... modify the Command as needed, but beware that the interface is designed to take these parameters in this sequence.

### icinga passive check

Add a passive check to you host where your agent/filedaemon runs and which should be backed up.

Example preview from icinga director:

```
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

Beware that we have set dummy state to 2, which indicates a missing passive check as critical as it most likely indicates a backup which has not been completed at all.
Modify the check interval to the minimum time window in which you expect at least one backup to complete.

### Monitoring multiple backup jobs per host in 24h

Create one check per backup time window and prefix the service name with the scheduled time for the backup (for example "23:00:00") and use the --prefix-time option to change the submission service name dynamically to match the expected backup time.

