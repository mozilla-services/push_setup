# Push Full-Stack Setup

This project assembles the necessary CloudFormation and Python scripts
needed to deploy a complete Push stack with or without log processing for the
Push Messages API component (needed for the Push Developer Dashboard).

## Components

At it's core, Push is composed of two programs, one that holds connections open
to clients, and one that handles incoming messages. This is to handle separate
scaling concerns for holding millions of client connections. The core system is
called ``autopush``.

Other systems, such as the Push Developer Dashboard and Push Messages API run
independently of ``autopush`` and function by processing log output. Running
them requires the logging and log processing components to be deployed. Logs
can be accumulated into S3 using Firehose even if later processing isn't
desired.

## Prerequisites

- Python 2.7
- AWS credentials

For log processing with lambda and the push messages API:

- Properly configured private subnet per
  https://github.com/mozilla-services/push-processor/#lambda-vpc-access

Configuration help for CloudFormation Parameters:

ProcessorVPCId
    VPC that the processor subnets are in.

ProcessorSubnetIds
    Private subnet(s) you created. Used by Redis the Lambda function that
    processes S3 logs and saves messages to Redis. Due to access of S3, this
    subnet requires a NAT Gateway per the VPC access above.

MessagesSubnetId
    Subnets that should be allowed to access the Push Messages API, ie, the
    subnet(s) the Developer Dashboard is in.

MessageApiEC2Subnet
    Subnet to run the actual Push Messages EC2 instance in. This subnet must
    be on the same VPC as the subnets for the Processor and should be Internet
    accessible (not the NAT'd Processor Subnet).

## Creating a CloudFormation Config

Create a virtualenv, activate it, and install the requirements:

    $ virtualenv myenv
    $ source myenv/bin/activate
    $ pip install -r requirements.txt

You are now ready to create a CloudFormation config!

**Note**: The Push DynamoDB tables will **not be deleted** when shutting down
the stack. This is intentional so that you can upgrade a stack to a new version
without losing data. **Remember to retain the AutopushCryptoKey and
PushTablePrefix** when creating the stack for use in later deployments to use
existing data.

**If you are just testing the stack, remember to delete your tables that are
prefixed with PushTablePrefix after you are done!**

### Push Service Only

Run:

    $ python deploy.py push

Will output a CloudFormation template to stdout. If you'd like to use it, then
save it to a file, and upload it in the AWS Console to Create] Stack in AWS
CloudFormation:

    $ python deploy.py push > push_stack.cf

Then specify this file in the AWS CloudFormation UI during Create Stack.

Run this stack in us-east-1 using 2 t2.micro instances:

[![LaunchStack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/new?stackName=myPushStack&templateURL=https://s3.amazonaws.com/cloudformation-push-setup/push_server.cf)

**Note**: Before using the Launch Stack button here or below, remember that the
default crypto-key here is only suitable for testing. Real use should have a
new unique crypto-key, not this default.

### Push Service + Firehose Logging

Run:

    $ python deploy.py push --firehose

Run this stack in us-east-1 using 2 t2.micro instances:

[![LaunchStack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/new?stackName=myPushStack&templateURL=https://s3.amazonaws.com/cloudformation-push-setup/push_server_firehose.cf)


### Push Service + Firehose Logging + Push Messages API

Run:

    $ python deploy.ph push --processor

The CloudFormation Output shows the internal IP of the Push Message API service
that that the Developer Dashboard should be configured to access.

Remember to setup a private NAT VPC per the instructions here first:
https://github.com/mozilla-services/push-processor/#lambda-vpc-accessz

## Post Setup

There are some steps that may be required after the stack has been created.

Outline:

1. Add S3 Put Event to Processor Lambda Function
2. Setup ELB for SSL termination

If the stack included the Push Messages API, you will need to manually configure
the Lambda Processor function. It should be set to trigger off the Firehose
Logging Bucket's object creation event in the AWS Console for log processing to
work. The Firehose bucket name and Lambda Processor name are provided in the
CloudFormation Outputs after it runs to assist in this.

If you're testing with Firefox, the plain websocket host provided will not work
as Firefox requires a trusted websocket connection. An ELB with a valid SSL
certificate should be added that terminates SSL and proxies TCP to the Push
Connection Node.

## Teardown

When deleting the stack, Security Groups will frequently halt a complete
tear-down due to ENI's created for the Lambda function. This will require some
manual deletion in the AWS console, before the stack can then resume being
deleted.

### Complete Push Stack Outline

- EC2
    - Autopush                  -> Firehose - Push Messages
    - Autoendpoint              -> Firehose - Push Messages
    - Push Messages API
        - Same VPC subnet as Elasticache

- VPC
    - Private Subnet
        - Default route to NAT Gateway for S3 Access
        - Lambda Push Processor
    - Public Subnet
        - Push Messages API
        - ElastiCache Redis

- Security Groups
    - InternalRouter
    - PushConnectionNode
        - Inbound from any to 8080
        - Inbound from InternalRouter to internal routing ports
    - PushEndpointNode
        - Inbound from any to 8082
    - PushMessagesNode
        - Inbound from any to 8080
    - Elasticache
        - Inbound from PushMessagesNode to 6379

- Lambda
    - Push Processor
        - Same VPC subnet as Elasticache Redis
        - S3 Trigger off Push Messages

- DynamoDB
    - Autopush Tables
    - Push Messages Table

- Firehose
    - PushMessages              -> S3 - Push Messages

- S3 Buckets
    - Push Messages
    - Push Processor Settings
    - Push Processor Lambda Zips

- Elasticache
    - Redis

- IAM roles
    - Autopush
        - DynamoDB
            - Read/Write - Autopush Tables
            - Create Tables
        - Firehose
            - PutRecord/PutRecordBatch
    - PushProcessor
        - S3
            - Read/Write - Push Processor Settings
        - DynamoDB
            - Read/Write - Push Messages Table
        - AWSLambdaVPCAccessExecutionRole policy
    - PushMessagesFirehoseDelivery
        - S3
            - Read/Write - Push Messages Bucket
    - PushMessagesAPI
        - DynamoDB
            - Read/Write - Push Messages Table
