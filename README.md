# AWS SSH Utils

[![Version](https://img.shields.io/pypi/v/aws-ssh-utils.svg)](https://pypi.org/project/aws-ssh-utils/)
[![License](https://img.shields.io/pypi/l/aws-ssh-utils.svg)](#)
[![Supported Python Versions](https://img.shields.io/pypi/pyversions/aws-ssh-utils.svg)](https://pypi.org/project/aws-ssh-utils/)

```shell
uvx aws-ssh-utils

uvx aws-ssh-utils app  # optional - `app` is default.
uvx aws-ssh-utils ec2
uvx aws-ssh-utils emr
uvx aws-ssh-utils emr-all

# Also exposed as `aws_ssh`
uvx --from aws-ssh-utils aws_ssh
```

This allows you to interactively SSH to an EC2 instance, EMR instance, or all EMR instances with TMUX.

It utilizes [questionary](https://pypi.org/project/questionary/) to ask you which instance you want to connect to.

## App

Terminal UI to browse EC2 instances and EMR clusters, with SSM or SSH shells in tabs.
It's the default command, so `aws_ssh` and `aws_ssh -p my-profile` launch it too.
Each connection tries, in order: `~/.ssh/config`, a keyless AWS SSM shell, opkssh (via instance/cluster tags),
the instance's EC2 key found in `~/.ssh`, then your default keys/ssh-agent.
SSM uses `aws ssm start-session --target INSTANCE_ID` with the selected profile and region. It requires the AWS CLI,
`session-manager-plugin`, and Session Manager access to the instance, but no SSH key, SSH server, or inbound port 22.
The shell runs as the Session Manager configured user (usually `ssm-user`); SSH usernames and key options only apply
to SSH connections. The `ec2` and `emr` commands use the same connection order; `emr-all` still uses SSH in tmux.
For SSH connections, unknown host keys are accepted and appended to `~/.ssh/known_hosts`; changed host keys are rejected.

```shell
$ aws_ssh app --help
Usage: aws_ssh app [OPTIONS]

  Browse EC2 instances and EMR clusters, and open SSM or SSH shells in tabs.

Options:
  -p, --profile TEXT          Which AWS profile to use
  -r, --region TEXT           Which AWS region to use
  --scrollback INTEGER RANGE  Lines of scrollback per shell. Defaults to your
                              tmux or Windows Terminal setting, else 1000.
                              [x>=0]
  --help                      Show this message and exit.
```

Scroll back with the mouse wheel or `shift+PgUp`/`shift+PgDn`; typing jumps back to the prompt.
`F12` shows/hides the sidebar, `ctrl+w` closes a failed tab, `ctrl+q` quits. Every other key goes to the focused shell.
Logs are viewable with `textual console`.

## EC2

Select an instance from an interactive list. You can filter the instances by name.

```shell
$ aws_auth ec2 --help
Usage: aws_ssh ec2 [OPTIONS]

  Asks user which EC2 instance they want to connect to, then opens an
  interactive SSM or SSH session to the instance

Options:
  -p, --profile TEXT    Which AWS profile to use
  -r, --region TEXT     Which AWS region to use
  -u, --user TEXT       Which user to connect as
  --private / --public  Connect to the instance's private or public IP
  -k, --key-file FILE   Which key file to use to connect
  --scrollback INTEGER RANGE
                        Lines of scrollback per shell. Defaults to your tmux or
                        Windows Terminal setting, else 1000.  [x>=0]
  --help                Show this message and exit.
```

## EMR

Select the EMR cluster, instance group, and instance to connect to.

```shell
$ aws_ssh emr --help
Usage: aws_ssh emr [OPTIONS]

  Asks user which Cluster and EC2 instance they want to connect to, then opens an interactive SSM or SSH session to the instance

Options:
  -p, --profile TEXT    Which AWS profile to use
  -r, --region TEXT     Which AWS region to use
  -u, --user TEXT       Which user to connect as
  --private / --public  Connect to the instance's private or public IP
  -k, --key-file FILE   Which key file to use to connect
  --scrollback INTEGER RANGE
                        Lines of scrollback per shell. Defaults to your tmux or
                        Windows Terminal setting, else 1000.  [x>=0]
  --help                Show this message and exit.
```

## EMR All

Select the EMR cluster to connect to. Then creates a new TMUX session with a window per instance. Each window will have an SSH connection to that instance open.

```shell
$ aws_ssh emr-all --help
Usage: aws_ssh emr-all [OPTIONS]

  Asks user which Cluster and EC2 instance they want to connect to, Then
  prints a tmux cli statement that will open a new session with a window per
  ec2 instance with ssh shell already opened.

Options:
  -p, --profile TEXT    Which AWS profile to use
  -r, --region TEXT     Which AWS region to use
  -u, --user TEXT       Which user to connect as
  --private / --public  Connect to the instance's private or public IP
  -k, --key-file FILE   Which key file to use to connect
  --help                Show this message and exit.
```
