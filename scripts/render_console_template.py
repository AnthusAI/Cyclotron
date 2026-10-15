"""Render the reviewable SAM backend; no AWS calls or permission activation."""
import json
from pathlib import Path


def ref(name):return {'Ref':name}
def sub(value):return {'Fn::Sub':value}
def attr(name,value='Arn'):return {'Fn::GetAtt':[name,value]}
def allow(actions,resources,condition=None):
    value={'Effect':'Allow','Action':actions,'Resource':resources}
    if condition:value['Condition']=condition
    return value

def template():
    resources={}
    for kind,name in [('Books','books'),('Auth','auth')]:
        resources[kind+'Bucket']={'Type':'AWS::S3::Bucket','DeletionPolicy':'Retain','UpdateReplacePolicy':'Retain','Properties':{
            'BucketName':sub('cyclotron-console-'+name+'-${AWS::AccountId}-${AWS::Region}'),
            'VersioningConfiguration':{'Status':'Enabled'},'OwnershipControls':{'Rules':[{'ObjectOwnership':'BucketOwnerEnforced'}]},
            'PublicAccessBlockConfiguration':{k:True for k in ('BlockPublicAcls','IgnorePublicAcls','BlockPublicPolicy','RestrictPublicBuckets')},
            'BucketEncryption':{'ServerSideEncryptionConfiguration':[{'ServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]}}}
    resources['SessionEncryptionKey']={'Type':'AWS::KMS::Key','DeletionPolicy':'Retain','UpdateReplacePolicy':'Retain','Properties':{
        'Description':'Cyclotron console envelope encryption; context-scoped runtime access','EnableKeyRotation':True,
        'KeyPolicy':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'AWS':sub('arn:${AWS::Partition}:iam::${AWS::AccountId}:root')},'Action':'kms:*','Resource':'*'}]}}}
    resources['SessionEncryptionAlias']={'Type':'AWS::KMS::Alias','Properties':{'AliasName':'alias/cyclotron-console-sessions','TargetKeyId':ref('SessionEncryptionKey')}}
    resources['CommandDLQ']={'Type':'AWS::SQS::Queue','Properties':{'QueueName':'cyclotron-console-commands-dlq','SqsManagedSseEnabled':True,'MessageRetentionPeriod':1209600}}
    resources['CommandQueue']={'Type':'AWS::SQS::Queue','Properties':{'QueueName':'cyclotron-console-commands','SqsManagedSseEnabled':True,
        'VisibilityTimeout':1080,'MessageRetentionPeriod':345600,'RedrivePolicy':{'deadLetterTargetArn':attr('CommandDLQ'),'maxReceiveCount':3}}}
    central='arn:aws:execute-api:us-east-1:712236451410:bnh9mctvo9/api'
    bff_paths=['GET/v1/identities/me','POST/v1/identities/google/challenge','POST/v1/identities/google/resolve',
        'GET/v1/accounts','POST/v1/accounts','GET/v1/accounts/*','POST/v1/accounts/*/authorize']
    worker_paths=['GET/v1/identities/me','POST/v1/accounts/*/authorize']
    for component in ('Bff','Worker'):
        suffix='bff' if component=='Bff' else 'worker'
        books=[sub('arn:${AWS::Partition}:s3:::${BooksBucket}/*/console/payloads/*')]
        domains=['console'] if component=='Bff' else ['console','workspace','runtime','shared-batches','evaluation','reviewer','optimizer']
        books += [sub('arn:${AWS::Partition}:s3:::${BooksBucket}/*/console/coordination/'+domain+'/*') for domain in domains]
        auth_payload=sub('arn:${AWS::Partition}:s3:::${AuthBucket}/console-auth/sessions/payloads/*')
        auth_heads=sub('arn:${AWS::Partition}:s3:::${AuthBucket}/console-auth/sessions/coordination/oauth/'+('*' if component=='Bff' else 'session-*/*'))
        statements=[allow(['s3:GetObject','s3:PutObject'],books),allow(['s3:GetObject']+(['s3:PutObject'] if component=='Bff' else []),[auth_payload,auth_heads])]
        statements.append(allow(['kms:Decrypt']+(['kms:GenerateDataKey'] if component=='Bff' else []),attr('SessionEncryptionKey'),{
            'StringEquals':{'kms:EncryptionContext:application':'cyclotron-console','kms:EncryptionContext:purpose':'oauth-session',
                            'kms:EncryptionContext:kind':['pending','session'] if component=='Bff' else 'session'},
            'StringLike':{'kms:EncryptionContext:record':'?'*64}}))
        statements.append(allow('execute-api:Invoke',[central+'/'+path for path in (bff_paths if component=='Bff' else worker_paths)]))
        statements.append(allow(['sqs:SendMessage'] if component=='Bff' else ['sqs:ReceiveMessage','sqs:DeleteMessage','sqs:ChangeMessageVisibility','sqs:GetQueueAttributes'],attr('CommandQueue')))
        statements.append(allow(['logs:CreateLogStream','logs:PutLogEvents'],sub('arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/cyclotron-console-'+suffix+':*')))
        resources[component+'Role']={'Type':'AWS::IAM::Role','Properties':{'RoleName':'cyclotron-console-'+suffix+'-runtime',
            'AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'lambda.amazonaws.com'},'Action':'sts:AssumeRole'}]},
            'Policies':[{'PolicyName':'cyclotron-console-'+suffix,'PolicyDocument':{'Version':'2012-10-17','Statement':statements}}]}}
        resources[component+'LogGroup']={'Type':'AWS::Logs::LogGroup','Properties':{'LogGroupName':'/aws/lambda/cyclotron-console-'+suffix,'RetentionInDays':14}}
        environment={
            'CYCLOTRON_ISSUER':ref('ConsoleIssuer'),'CYCLOTRON_CLIENT_ID':ref('ConsoleClientId'),
            'CYCLOTRON_AUTHORIZATION_ORIGIN':ref('ConsoleAuthorizationOrigin'),'CYCLOTRON_PUBLIC_ORIGIN':'https://lab.cyclotron.anth.us',
            'CYCLOTRON_ACCOUNT':ref('AWS::AccountId'),'CYCLOTRON_REGION':ref('AWS::Region'),
            'CYCLOTRON_BOOKS_BUCKET':ref('BooksBucket'),'CYCLOTRON_AUTH_BUCKET':ref('AuthBucket'),
            'CYCLOTRON_SESSION_KEY_ARN':attr('SessionEncryptionKey'),'CYCLOTRON_QUEUE_URL':ref('CommandQueue')}
        properties={'FunctionName':'cyclotron-console-'+suffix,'Runtime':'python3.13','Architectures':['x86_64'],
            'Handler':'decision_flywheel.cloud_entry.'+('bff_handler' if component=='Bff' else 'worker_handler'),
            'CodeUri':'./console-backend.zip','Role':attr(component+'Role'),'MemorySize':1024,
            'Timeout':28 if component=='Bff' else 180,'ReservedConcurrentExecutions':5 if component=='Bff' else 2,
            'Environment':{'Variables':environment},'Tags':{'Product':'cyclotron','FederationReview':ref('FederationReviewId'),'ExecutionPairReview':ref('ExecutionPairReviewId')}}
        if component=='Bff':
            properties['Events']={name:{'Type':'HttpApi','Properties':{'ApiId':ref('ConsoleApi'),'Path':path,'Method':'ANY','PayloadFormatVersion':'2.0'}} for name,path in [('Root','/'),('Proxy','/{proxy+}')]}
        else:properties['Events']={'Commands':{'Type':'SQS','Properties':{'Queue':attr('CommandQueue'),'BatchSize':1,'FunctionResponseTypes':['ReportBatchItemFailures'],'ScalingConfig':{'MaximumConcurrency':2}}}}
        resources[component+'Function']={'Type':'AWS::Serverless::Function','DependsOn':component+'LogGroup','Properties':properties}
    for kind in ('Books','Auth'):
        bucket=kind+'Bucket';prefix=sub('arn:${AWS::Partition}:s3:::${'+bucket+'}/*')
        create_only=[sub('arn:${AWS::Partition}:s3:::${'+bucket+'}/*/payloads/*'),sub('arn:${AWS::Partition}:s3:::${'+bucket+'}/*/coordination/*/*/anchor.json')]
        heads=sub('arn:${AWS::Partition}:s3:::${'+bucket+'}/*/coordination/*/*/head.json')
        resources[kind+'BucketPolicy']={'Type':'AWS::S3::BucketPolicy','Properties':{'Bucket':ref(bucket),'PolicyDocument':{'Version':'2012-10-17','Statement':[
            {'Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[sub('arn:${AWS::Partition}:s3:::${'+bucket+'}'),prefix],'Condition':{'Bool':{'aws:SecureTransport':'false'}}},
            {'Effect':'Deny','Principal':'*','Action':'s3:PutObject','Resource':create_only,'Condition':{'StringNotEquals':{'s3:if-none-match':'*'}}},
            {'Effect':'Deny','Principal':'*','Action':'s3:PutObject','Resource':heads,'Condition':{'Null':{'s3:if-match':'true','s3:if-none-match':'true'}}}
        ]}}}
    resources['ConsoleApi']={'Type':'AWS::Serverless::HttpApi','Properties':{'StageName':'$default','DefaultRouteSettings':{'ThrottlingBurstLimit':10,'ThrottlingRateLimit':5}}}
    return {'AWSTemplateFormatVersion':'2010-09-09','Transform':'AWS::Serverless-2016-10-31',
        'Description':'Reviewed Cyclotron console S3 backend; separate BFF and worker roles; live providers disabled',
        'Parameters':{
            'ConsoleIssuer':{'Type':'String','Default':'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_hlSyajw9S'},
            'ConsoleClientId':{'Type':'String','AllowedPattern':'[a-z0-9]{1,128}','Description':'Dedicated reviewed console OAuth client; never directory/CRM client'},
            'ConsoleAuthorizationOrigin':{'Type':'String','AllowedPattern':'https://[a-z0-9.-]+','Description':'Existing central managed-login origin'},
            'FederationReviewId':{'Type':'String','AllowedValues':['cyclotron-google-only-unlinked-v1-20261015']},
            'ExecutionPairReviewId':{'Type':'String','AllowedValues':['cyclotron-execution-pair-v1-20261015']}},
        'Resources':resources,'Outputs':{'BackendOrigin':{'Value':sub('https://${ConsoleApi}.execute-api.${AWS::Region}.amazonaws.com')},
            'BffRoleArn':{'Value':attr('BffRole')},'WorkerRoleArn':{'Value':attr('WorkerRole')},'SessionKeyArn':{'Value':attr('SessionEncryptionKey')},
            'BooksBucketName':{'Value':ref('BooksBucket')},'AuthBucketName':{'Value':ref('AuthBucket')},'CommandQueueUrl':{'Value':ref('CommandQueue')}}}

if __name__=='__main__':
    Path('deployment/template.json').write_text(json.dumps(template(),indent=2)+'\n')
