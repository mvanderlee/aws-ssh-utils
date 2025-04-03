# AWS SSH Utils

```shell
pip install aws-ssh-utils

aws_ssh ec2

aws_ssh emr

aws_ssh emr-all
```

This allows you to interactively SSH to an EC2 instance, EMR instance, or all EMR instances with TMUX.

It utilizes [questionary](https://pypi.org/project/questionary/) to ask you which instance you want to connect to.

## EC2

Select an instance from an interactive list. You can filter the instances by name.

```shell
$ aws_auth ec2 --help
Usage: aws_ssh ec2 [OPTIONS]

  Asks user which EC2 instance they want to connect to, then opens an
  interactive SSH session to the instance

Options:
  -p, --profile TEXT    Which AWS profile to use
  -r, --region TEXT     Which AWS region to use
  -u, --user TEXT       Which user to connect as
  --private / --public  Connect to the instance's private or public IP
  -k, --key-file TEXT   Which key file to use to connect
  --help                Show this message and exit.
```

## EMR

Select the EMR cluster, instance group, and instance to connect to.

```shell
$ aws_ssh emr --help
Usage: aws_ssh emr [OPTIONS]

  Asks user which Cluster and EC2 instance they want to connect to, then opens an interactive SSH session to the instance

Options:
  -p, --profile TEXT    Which AWS profile to use
  -r, --region TEXT     Which AWS region to use
  -u, --user TEXT       Which user to connect as
  --private / --public  Connect to the instance's private or public IP
  -k, --key-file TEXT   Which key file to use to connect
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
  -k, --key-file TEXT   Which key file to use to connect
  --help                Show this message and exit.
```
