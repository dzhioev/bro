#!/usr/bin/env python3

import aws_cdk as cdk
import boto3

from bro.oops.cdk import create_app, resolve

application = cdk.App()
raw_platform_only = application.node.try_get_context('platform-only')
if raw_platform_only not in (None, True, False, 'true', 'false'):
  raise ValueError('CDK context platform-only must be true or false')
platform_only = raw_platform_only in (True, 'true')

infrastructure_config = resolve()
account = boto3.client('sts', region_name=infrastructure_config.region).get_caller_identity()[
  'Account'
]
create_app(
  infrastructure_config,
  account,
  app=application,
  platform_only=platform_only,
)
application.synth()
