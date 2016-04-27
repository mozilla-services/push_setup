"""Lambda S3 Writer CloudFormation Custom resoure

Note that some portions of this are copy/paste from:
https://github.com/humilis/humilis-firehose-resource/

As such those portions of code are MIT licensed per:
https://github.com/humilis/humilis-firehose-resource/blob/master/LICENSE

"""
from __future__ import print_function

import base64
import json
import sys
import urllib2
import StringIO

import boto3


SUCCESS = "SUCCESS"
FAILED = "FAILED"
FINAL_STATES = ['ACTIVE']


def send(event, context, response_status, reason=None, response_data=None,
         physical_resource_id=None):
    response_data = response_data or {}
    reason = reason or "See the details in CloudWatch Log Stream: " + \
        context.log_stream_name
    physical_resource_id = physical_resource_id or context.log_stream_name
    response_body = json.dumps(
        {
            'Status': response_status,
            'Reason': reason,
            'PhysicalResourceId': physical_resource_id,
            'StackId': event['StackId'],
            'RequestId': event['RequestId'],
            'LogicalResourceId': event['LogicalResourceId'],
            'Data': response_data
        }
    )

    opener = urllib2.build_opener(urllib2.HTTPHandler)
    request = urllib2.Request(event["ResponseURL"], data=response_body)
    request.add_header("Content-Type", "")
    request.add_header("Content-Length", len(response_body))
    request.get_method = lambda: 'PUT'
    try:
        response = opener.open(request)
        print("Status code: {}".format(response.getcode()))
        print("Status message: {}".format(response.msg))
        return True
    except urllib2.HTTPError as exc:
        print("Failed executing HTTP request: {}".format(exc.code))
        return False


def create_file(event, context):
    """Create a S3 file

    If the Content is a dict, it will be JSON serialized to S3.
    Otherwise the Content is assumed to be base64 encoded and will
    be decoded before it is written.

    """
    filecfg = event["ResourceProperties"]
    s3_bucket = filecfg["Bucket"]
    s3_key = filecfg["Key"]
    content = filecfg["Content"]
    file_type = "text/plain"
    if isinstance(content, dict):
        file_type = "application/json"
        content = json.dumps(content)
    else:
        content = base64.urlsafe_b64decode(content)

    s3 = boto3.resource("s3")
    data = StringIO.StringIO(content)
    s3.Bucket(s3_bucket).put_object(Key=s3_key, ContentType=file_type,
                                    Body=data)
    arn = "arn:aws:s3:::{}:{}".format(s3_bucket, s3_key)

    send(event, context, SUCCESS, physical_resource_id=arn,
         response_data={"Arn": arn})


def delete_file(event, context):
    """Delete a S3 file"""
    s3_arn = event["PhysicalResourceId"]
    bucket, key = s3_arn.split(":")[-2:]
    s3 = boto3.resource("s3")
    s3.Bucket(bucket).delete_objects(
        Delete={
            "Objects": [
                {"Key": key}
            ]
        },
    )
    send(event, context, SUCCESS, response_data={})


HANDLERS = {
    "Delete": delete_file,
    "Update": create_file,
    "Create": create_file
}


def lambda_handler(event, context):
    handler = HANDLERS.get(event["RequestType"])
    try:
        return handler(event, context)
    except:
        msg = ""
        for err in sys.exc_info():
            msg += "\n{}\n".format(err)
        response_data = {
            "Error": "{} resource failed: {}".format(event["RequestType"], msg)
        }
        print(response_data)
        return send(event, context, FAILED, response_data=response_data)
        raise
