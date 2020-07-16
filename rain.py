#!/usr/bin/env python
import argparse
import datetime
import difflib
import json
import pprint
import os
import sys
import time

import boto3
import botocore.exceptions
import json_tools

from templates import TEMPLATES
from config import constants


def confirm_choice(message):
    # raw_input returns the empty string for "enter"
    yes = set(['yes','y', 'ye', ''])
    no = set(['no','n'])

    sys.stdout.write(message)
    choice = input().lower().strip()
    if choice in yes:
        return True
    elif choice in no:
        return False
    else:
        sys.stdout.write("Please respond with 'yes' or 'no'")


def print_reduced(diff):
    """ Prints JSON diff in reduced format (similar to plain diffs).
    """
    for action in diff:
        if 'add' in action:
            print('+ {}:\n{}'.format(action['add'], json.dumps(action['value'], indent=4)))
        elif 'remove' in action:
            print('- {}:\n{}'.format(action['remove'], json.dumps(action['prev'], indent=4)))
        elif 'replace' in action:
            prev = json.dumps(action['prev'])
            new = json.dumps(action['value'])
            print('{}:\n'.format(action['replace']))
            # try:
            for line in difflib.ndiff((prev, ), (new, )):
                print(line)
            # except Exception as e:
            #     print(action)
            #     print('? {}:\n{}'.format(action['add'], json.dumps(action['value'], indent=4)))
        print("\n-----------------------------------\n")


def wait_for_completion(env, stack_id):
    conn = boto3.client('cloudformation', region_name=constants.ENVIRONMENTS[env]['region'])
    print('Waiting for stack {}...'.format(stack_id))
    last_event = conn.describe_stack_events(StackName=stack_id)['StackEvents'][0]
    failed = False
    while not (last_event['ResourceType'] == 'AWS::CloudFormation::Stack' and
               last_event['ResourceStatus'] in ['CREATE_FAILED', 'CREATE_COMPLETE', 'DELETE_FAILED',
                                                'DELETE_COMPLETE', 'ROLLBACK_COMPLETE', 'UPDATE_FAILED',
                                                'UPDATE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']):
        if last_event['ResourceStatus'].endswith('FAILED'):
            failed = True
        time.sleep(1)
        last_event = conn.describe_stack_events(StackName=stack_id)['StackEvents'][0]
        print('{} StackId: {}, Resource: {ResourceType}, Status: {ResourceStatus}'.
              format(datetime.datetime.now().isoformat(), stack_id, **last_event))
    if failed:
        print('*** Stack apply failed! ***')
    else:
        print('Stack action complete.')


def list_templates():
    """
    List all Troposphere Templates
    """
    return TEMPLATES


def list_stacks(env, stack_status_filters=None):
    """
    List all existing stacks and their statuses
    """
    if stack_status_filters is None:
        stack_status_filters = []

    conn = boto3.client('cloudformation', region_name=constants.ENVIRONMENTS[env]['region'])
    result = conn.list_stacks(StackStatusFilter=stack_status_filters)
    stack_summaries = result['StackSummaries']
    while result.get('NextToken'):
        token = result.get('NextToken')
        result = conn.list_stacks(NextToken=token, StackStatusFilter=stack_status_filters)
        stack_summaries.extend(result['StackSummaries'])
    return stack_summaries


def confirm_action(f, *args, **kwargs):
    to_continue = confirm_choice("\n\nContinue? (yes/no) ")
    if to_continue:
        sys.stdout.write('Running in ')
        sys.stdout.flush()
        for i in range(5, 0, -1):
            sys.stdout.write('{}...'.format(i))
            sys.stdout.flush()
            time.sleep(1)
        print("\n")
        return f(*args, **kwargs)
    else:
        print("Cancelled.\n")
        sys.exit(0)


def apply_stack(env, template_name, params={}):
    cfn_conn = boto3.client('cloudformation', region_name=constants.ENVIRONMENTS[env]['region'])
    s3_conn = boto3.client('s3', region_name=constants.ENVIRONMENTS[env]['region'])
    TemplateClass = TEMPLATES.get(template_name, None)
    if not TemplateClass:
        raise RuntimeError('{} not a valid Template Class'.format(template_name))
    template = TemplateClass(template_name, env, params)
    stack_args = {
        'Capabilities': template.CAPABILITIES,
        'Parameters': [
            {
                'ParameterKey': k,
                'ParameterValue': v,
                'UsePreviousValue': False
            } for k, v in params.items()
        ],
        'StackName': '{}-{}'.format(env, template_name),
        'Tags': [
            {'Key': '{}:team'.format(constants.TAG), 'Value': template.TEAM['email']},
            {'Key': '{}:environment'.format(constants.TAG), 'Value': env}
        ],
    }
    if len(template.to_json()) < 51200:
        stack_args['TemplateBody'] = template.to_json()
    else:
        bucket = '{}-{}-infra'.format(constants.TAG, env)
        key = 'cfn/{}/{}-{}'.format(env, datetime.datetime.now().strftime('%Y%m%d-%H:%M'), template_name)
        s3_conn.put_object(
            Body=template.to_json(),
            Bucket=bucket,
            ContentType='application/json',
            Key=key
        )
        stack_args['TemplateURL'] = 'https://s3.dualstack.{}.amazonaws.com/{}/{}'.format(
            constants.ENVIRONMENTS[env]['region'], bucket, key)
    if template:
        if stack_args['StackName'] in [s['StackName']
                                       for s in list_stacks(env)
                                       if s['StackStatus'] != 'DELETE_COMPLETE']:
            # stack exists, update
            stack_args.pop('Tags', None)  # update_stack can't take Tags
            old = json.loads(json.dumps(cfn_conn.get_template(**{'StackName': stack_args['StackName']})['TemplateBody']))
            new = json.loads(template.to_json())
            print("Proposed changes:")
            print_reduced(json_tools.diff(old, new))
            try:
                response = confirm_action(cfn_conn.update_stack, **stack_args)
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == 'ValidationError':
                    print(e.response['Error']['Message'])
                    sys.exit(0)
            except:
                raise
        else:
            # Create a new stack
            print('Creating a new stack: {}'.format(stack_args['StackName']))
            print('Template:')
            print(template.to_json())
            response = confirm_action(cfn_conn.create_stack, **stack_args)

        wait_for_completion(env, response['StackId'])


def show_template(env, template_name, params={}):
    TemplateClass = TEMPLATES.get(template_name, None)
    template = TemplateClass(template_name, env, params)
    print('=========== Environment: [{}], Template: [{}] ==========='.format(env, template_name))
    print(template.to_json())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Wrapper around boto and troposphere to manage cloudformation')
    parser.add_argument('environment', nargs='?', const=1, default=os.environ.get('ENV', 'dev'),
                        choices=constants.ENVIRONMENTS.keys(), help='Environment to run')
    parser.add_argument('action', choices=['templates', 'stacks', 'show', 'apply'])
    parser.add_argument('--template')
    parser.add_argument('--parameters')
    args = parser.parse_args()

    params = {}
    if args.parameters:
        for param in args.parameters.split(','):
            k, v = param.split('=')
            params[k] = v

    if args.action == 'templates':
        pprint.pprint(list_templates())
    elif args.action == 'stacks':
        for stack in list_stacks(args.environment):
            pprint.pprint(stack)
    elif args.action == 'show':
        show_template(args.environment, args.template, params)
    elif args.action == 'apply':
        print('Env: {} applying template: {}'.format(args.environment, args.template))
        apply_stack(args.environment, args.template, params)
