#!/usr/bin/env python3

import aws_cdk as cdk
import boto3

from bro.oops.cdk import create_app, resolve

application = cdk.App()
infrastructure_config = resolve()
account = boto3.client('sts', region_name=infrastructure_config.region).get_caller_identity()[
  'Account'
]
create_app(infrastructure_config, account, app=application)
application.synth()
