import uuid

import awacs.dynamodb as ddb
import awacs.elasticache as elasticache
import awacs.ec2 as ec2
import awacs.s3 as s3
import click
from awacs.aws import (
    Action,
    Allow,
    Statement,
    Principal,
    Policy
)
from awacs.sts import AssumeRole
from cryptography.fernet import Fernet
from troposphere import (
    Base64,
    GetAtt,
    Join,
    Parameter,
    Ref,
    Output,
    Tags,
    Template,
)
from troposphere.awslambda import (
    Code,
    Function,
    VPCConfig,
)
from troposphere.cloudformation import CustomResource
from troposphere.ec2 import (
    Instance,
    SecurityGroup,
    SecurityGroupRule,
)
from troposphere.elasticache import (
    CacheCluster,
    SubnetGroup,
)
from troposphere.iam import (
    InstanceProfile,
    PolicyType,
    Role,
)
from troposphere.policies import (
    CreationPolicy,
    ResourceSignal,
)
from troposphere.s3 import (
    Bucket,
)


@click.group()
def cli():
    pass


@click.command()
@click.option("--firehose/--no-firehose", default=False,
              help="Include Firehose output")
@click.option("--processor/--no-processor", default=False,
              help="Include Message processing and API, includes firehose")
def push(firehose, processor):
    cb = CloudFormationBuilder(use_firehose=firehose,
                               use_processor=processor)
    print cb.json()


cli.add_command(push)


# Common bits
ref_stack_id = Ref('AWS::StackId')


def allow_tcp(port, from_ip="0.0.0.0/0"):
    return SecurityGroupRule(
        IpProtocol="tcp",
        FromPort=port,
        ToPort=port,
        CidrIp=from_ip
    )


class CloudFormationBuilder(object):
    def __init__(self, use_firehose=False, use_processor=False):
        self._random_id = str(uuid.uuid4()).replace('-', '')[:12].upper()
        self._template = Template()
        self._template.add_version("2010-09-09")
        desc = "AWS CloudFormation Push Stack"
        if use_processor:
            desc += " - with Firehose Logging + Processor + Push Messages API"
        elif use_firehose:
            desc += " - with Firehose Logging"
        self._template.add_description(desc)
        self.use_firehose = use_firehose or use_processor
        self.use_processor = use_processor
        self.add_resource = self._template.add_resource
        self.add_parameter = self._template.add_parameter

        self.AutopushVersion = self.add_parameter(Parameter(
            "AutopushVersion",
            Type="String",
            Description="Autopush version to deploy",
            Default="1.14.2",
            AllowedValues=[
                "latest",
                "1.14.2",
                "1.14.1",
            ]
        ))

        if self.use_processor:
            self.ProcessorLambdaBucket = self.add_parameter(Parameter(
                "ProcessorLambdaBucket",
                Type="String",
                Default="push-lambda-funcs",
                Description="S3 Bucket of lambda Message Processor",
            ))
            self.ProcessorLambdaKey = self.add_parameter(Parameter(
                "ProcessorLambdaKey",
                Type="String",
                Default="push_processor_0.4.zip",
                Description="S3 Key of lambda Message Processor",
            ))
            self.ProcessorVPCId = self.add_parameter(Parameter(
                "ProcessorVPCId",
                Type="AWS::EC2::VPC::Id",
                Description="VPC to run the processor/Messages API in"
            ))
            self.ProcessorSubnetIds = self.add_parameter(Parameter(
                "ProcessorSubnetIds",
                Type="List<AWS::EC2::Subnet::Id>",
                Description=(
                    "Processor Subnet ID's, MUST have NAT Gateway "
                    "access for S3 loading"
                ),
            ))
            self.MessagesSecurityGroup = self.add_parameter(Parameter(
                "MessagesSecurityGroup",
                Type="AWS::EC2::SecurityGroup::Id",
                Description="Security Group to allow Messages API access from",
            ))
            self.MessageAPISubnetId = self.add_parameter(Parameter(
                "MessageApiEC2Subnet",
                Type="AWS::EC2::Subnet::Id",
                Description=(
                    "Subnet to run Push Messages EC2 Instance in, MUST "
                    "be in the same VPC as the Processor Subnets"
                )
            ))
            self.PushMessagesVersion = self.add_parameter(Parameter(
                "PushMessagesVersion",
                Type="String",
                Description="Push-Messages API version to deploy",
                Default="0.6",
                AllowedValues=[
                    "latest",
                    "0.5",
                    "0.6"
                ]
            ))

        self.PushCryptoKey = self.add_parameter(Parameter(
            "AutopushCryptoKey",
            Type="String",
            Default=Fernet.generate_key(),
            Description="Autopush crypto-key",
            MinLength=44,
            MaxLength=44,
        ))
        self.KeyPair = self.add_parameter(Parameter(
            "AutopushSSHKeyPair",
            Type="AWS::EC2::KeyPair::KeyName",
            Description="Name of an EC2 KeyPair to enable SSH access."
        ))
        self.PushTablePrefix = self.add_parameter(Parameter(
            "PushTablePrefix",
            Type="String",
            Default="autopush_" + self._random_id,
            Description="Autopush DynamoDB Table Prefixes",
        ))

        if self.use_firehose:
            self._setup_firehose_custom_resource()
            self._add_firehose()

        self._add_autopush_security_group()
        self._add_autopush_iam_roles()
        self._add_autopush_servers()

        if self.use_processor:
            self._add_processor_databases()
            self._setup_s3writer_custom_resource()
            self._add_processor()
            self._add_push_messages_api()

    def _add_autopush_security_group(self):
        self.InternalRouterSG = self.add_resource(SecurityGroup(
            "AutopushInternalRouter",
            GroupDescription="Internal Routing SG"
        ))
        self.EndpointSG = self.add_resource(SecurityGroup(
            "AutopushEndpointNode",
            SecurityGroupIngress=[
                allow_tcp(8082),
                allow_tcp(22),
            ],
            GroupDescription="Allow HTTP traffic to autoendpoint node",
        ))
        self.ConnectionSG = self.add_resource(SecurityGroup(
            "AutopushConnectionNode",
            SecurityGroupIngress=[
                allow_tcp(8080),
                allow_tcp(22),
                SecurityGroupRule(
                    IpProtocol="tcp",
                    FromPort=8081,
                    ToPort=8081,
                    SourceSecurityGroupName=Ref(self.InternalRouterSG)
                )
            ],
            GroupDescription=(
                "Allow Websocket traffic to autopush node"
            )
        ))

    def _add_autopush_iam_roles(self):
        firehose_extras = []
        if self.use_firehose:
            # Add in the firehose permissions
            firehose_extras.append(Statement(
                Effect=Allow,
                Action=[
                    Action("firehose", "PutRecord"),
                    Action("firehose", "PutRecordBatch"),
                ],
                Resource=[
                    GetAtt(self.FirehoseLogstream, "Arn"),
                ]
            ))
        self.PushServerRole = self.add_resource(Role(
            "AutopushServerRole",
            AssumeRolePolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[AssumeRole],
                        Principal=Principal("Service", "ec2.amazonaws.com")
                    )
                ]
            ),
            Path="/",
        ))
        self.add_resource(PolicyType(
            "AutopushServerRolePolicy",
            PolicyName="AutopushServerRole",
            PolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[
                            ddb.BatchGetItem,
                            ddb.BatchWriteItem,
                            ddb.GetItem,
                            ddb.PutItem,
                            ddb.DeleteItem,
                            ddb.UpdateItem,
                            ddb.Query,
                            ddb.Scan,
                        ],
                        Resource=[
                            Join("", ["arn:aws:dynamodb:us-east-1:*:table/",
                                      Ref(self.PushTablePrefix),
                                      "_*"]
                                 )
                        ]
                    ),
                    Statement(
                        Effect=Allow,
                        Action=[
                            ddb.ListTables,
                            ddb.DescribeTable,
                            ddb.CreateTable,
                        ],
                        Resource=["*"]
                    )
                ] + firehose_extras
            ),
            Roles=[Ref(self.PushServerRole)]
        ))

    def _aws_cfn_signal_service(self, wait_for, resource):
        """Returns an array suitable to join for UserData that signals after
        the wait_for service has started
        """
        return [
            "    - name: 'aws_cfn_signal.service'\n",
            "      command: 'start'\n",
            "      content: |\n",
            "        [Unit]\n",
            "        Description=AWS Cloud Formation Signaling\n",
            "        After=%s.service\n" % wait_for,
            "        Requires=%s.service\n" % wait_for,
            "        Type=oneshot\n",
            "        \n",
            "        [Service]\n",
            "        TimeoutStartSec=0\n",
            "        EnvironmentFile=/etc/environment\n",
            "        ExecStartPre=/usr/bin/docker pull ",
            "aweber/cfn-signal\n",
            "        ExecStart=/usr/bin/docker run --name cfn-signal ",
            "aweber/cfn-signal --success=true --reason='Registry Started'",
            " --stack=", Ref("AWS::StackName"),
            " --resource=", resource, "\n",
        ]

    def _instance_tags(self, app, app_type):
        return Tags(
            App=app,
            Datadog="false",
            Env="testing",
            Name=Join("", [Ref("AWS::StackName"), "-", app_type]),
            Stack=Ref("AWS::StackName"),
            Type=app_type,
        )

    def _add_autopush_servers(self):
        self.PushServerInstanceProfile = self.add_resource(InstanceProfile(
            "AutopushServerInstanceProfile",
            Path="/",
            Roles=[Ref(self.PushServerRole)]
        ))
        # Extras is common options for UserData
        extras = [
            "--hostname $public_ipv4 ",
            "--storage_tablename ", Ref(self.PushTablePrefix), "_storage ",
            "--message_tablename ", Ref(self.PushTablePrefix), "_message ",
            "--router_tablename ", Ref(self.PushTablePrefix), "_router ",
            "--crypto_key '", Ref(self.PushCryptoKey), "' ",
        ]
        if self.use_firehose:
            extras.extend([
                "--firehose_stream_name ", Ref(self.FirehoseLogstream), " "
            ])
        self.PushEndpointServerInstance = self.add_resource(Instance(
            "AutopushEndpointInstance",
            ImageId="ami-2c393546",
            InstanceType="t2.micro",
            SecurityGroups=[
                Ref(self.EndpointSG),
                Ref(self.InternalRouterSG),
            ],
            KeyName=Ref(self.KeyPair),
            IamInstanceProfile=Ref(self.PushServerInstanceProfile),
            CreationPolicy=CreationPolicy(
                ResourceSignal=ResourceSignal(
                    Timeout='PT15M'
                )
            ),
            UserData=Base64(Join("", [
                "#cloud-config\n\n",
                "coreos:\n",
                "  units:\n",
                ] + self._aws_cfn_signal_service(
                    "autoendpoint", "AutopushEndpointInstance") + [
                "    - name: 'autoendpoint.service'\n",
                "      command: 'start'\n",
                "      content: |\n",
                "        [Unit]\n",
                "        Description=Autoendpoint container\n",
                "        Author=Mozilla Services\n",
                "        After=docker.service\n",
                "        \n",
                "        [Service]\n",
                "        Restart=always\n",
                "        ExecStartPre=-/usr/bin/docker kill autoendpoint\n",
                "        ExecStartPre=-/usr/bin/docker rm autoendpoint\n",
                "        ExecStartPre=/usr/bin/docker pull ",
                "bbangert/autopush:", Ref(self.AutopushVersion), "\n",
                "        ExecStart=/usr/bin/docker run ",
                "--name autoendpoint ",
                "-p 8082:8082 ",
                "-e 'AWS_DEFAULT_REGION=us-east-1' ",
                "bbangert/autopush:", Ref(self.AutopushVersion), " ",
                "./pypy/bin/autoendpoint ",
            ] + extras)),
            DependsOn="AutopushServerRolePolicy",
            Tags=self._instance_tags("autopush", "autoendpoint"),
        ))
        self.PushConnectionServerInstance = self.add_resource(Instance(
            "AutopushConnectionInstance",
            ImageId="ami-2c393546",
            InstanceType="t2.micro",
            SecurityGroups=[
                Ref(self.ConnectionSG),
                Ref(self.InternalRouterSG),
            ],
            KeyName=Ref(self.KeyPair),
            IamInstanceProfile=Ref(self.PushServerInstanceProfile),
            CreationPolicy=CreationPolicy(
                ResourceSignal=ResourceSignal(
                    Timeout='PT15M'
                )
            ),
            UserData=Base64(Join("", [
                "#cloud-config\n\n",
                "coreos:\n",
                "  units:\n",
                ] + self._aws_cfn_signal_service(
                    "autopush", "AutopushConnectionInstance") + [
                "    - name: 'autopush.service'\n",
                "      command: 'start'\n",
                "      content: |\n",
                "        [Unit]\n",
                "        Description=Autopush container\n",
                "        Author=Mozilla Services\n",
                "        After=docker.service\n",
                "        \n",
                "        [Service]\n",
                "        Restart=always\n",
                "        ExecStartPre=-/usr/bin/docker kill autopush\n",
                "        ExecStartPre=-/usr/bin/docker rm autopush\n",
                "        ExecStartPre=/usr/bin/docker pull ",
                "bbangert/autopush:", Ref(self.AutopushVersion), "\n",
                "        ExecStart=/usr/bin/docker run ",
                "--name autopush ",
                "-p 8080:8080 ",
                "-p 8081:8081 ",
                "-e 'AWS_DEFAULT_REGION=us-east-1' ",
                "bbangert/autopush:", Ref(self.AutopushVersion), " ",
                "./pypy/bin/autopush ",
                "--router_hostname $private_ipv4 ",
                "--endpoint_hostname ",
                GetAtt(self.PushEndpointServerInstance, "PublicDnsName"),
                " ",
            ] + extras)),
            DependsOn="AutopushServerRolePolicy",
            Tags=self._instance_tags("autopush", "autopush"),
        ))
        self._template.add_output([
            Output(
                "PushServerURL",
                Description="Push Websocket URL",
                Value=Join("", [
                    "ws://",
                    GetAtt(self.PushConnectionServerInstance, "PublicDnsName"),
                    ":8080/"
                ])
            )
        ])

    def _setup_firehose_custom_resource(self):
        # Setup the FirehoseLambda CloudFormation Custom Resource
        self.FirehoseLambdaCFExecRole = self.add_resource(Role(
            "FirehoseLambdaCFRole",
            AssumeRolePolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[AssumeRole],
                        Principal=Principal("Service", "lambda.amazonaws.com")
                    )
                ]
            ),
            Path="/",
        ))
        self.FirehoseLambdaPolicy = self.add_resource(PolicyType(
            "FirehoseCFPolicy",
            PolicyName="FirehoseLambdaCFRole",
            PolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[
                            Action("logs", "CreateLogGroup"),
                            Action("logs", "CreateLogStream"),
                            Action("logs", "PutLogEvents"),
                        ],
                        Resource=[
                            "arn:aws:logs:*:*:*"
                        ]
                    ),
                    Statement(
                        Effect=Allow,
                        Action=[
                            Action("firehose", "CreateDeliveryStream"),
                            Action("firehose", "DeleteDeliveryStream"),
                            Action("firehose", "ListDeliveryStreams"),
                            Action("firehose", "DescribeDeliveryStream"),
                            Action("firehose", "UpdateDestination"),
                        ],
                        Resource=["*"]
                    )
                ]
            ),
            Roles=[Ref(self.FirehoseLambdaCFExecRole)],
            DependsOn="FirehoseLambdaCFRole"
        ))
        self.FirehoseCFCustomResource = self.add_resource(Function(
            "FirehoseCustomResource",
            Description=(
                "Creates, updates, and deletes Firehose delivery streams"
            ),
            Runtime="python2.7",
            Timeout=300,
            Handler="lambda_function.lambda_handler",
            Role=GetAtt(self.FirehoseLambdaCFExecRole, "Arn"),
            Code=Code(
                S3Bucket="cloudformation-custom-resources",
                S3Key="firehose_lambda.zip",
            ),
            DependsOn="FirehoseCFPolicy"
        ))

    def _setup_s3writer_custom_resource(self):
        self.S3WriterLambdaCFExecRole = self.add_resource(Role(
            "S3WriterLambdaCFRole",
            AssumeRolePolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[AssumeRole],
                        Principal=Principal("Service", "lambda.amazonaws.com")
                    )
                ]
            ),
            Path="/",
        ))
        self.S3WriterCFPolicy = self.add_resource(PolicyType(
            "S3WriterCFPolicy",
            PolicyName="S3WriterLambdaCFRole",
            PolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[
                            Action("logs", "CreateLogGroup"),
                            Action("logs", "CreateLogStream"),
                            Action("logs", "PutLogEvents"),
                        ],
                        Resource=[
                            "arn:aws:logs:*:*:*"
                        ]
                    ),
                    Statement(
                        Effect=Allow,
                        Action=[
                            s3.DeleteObject,
                            s3.ListBucket,
                            s3.PutObject,
                            s3.GetObject,
                        ],
                        Resource=["*"]
                    )
                ]
            ),
            Roles=[Ref(self.S3WriterLambdaCFExecRole)],
            DependsOn="S3WriterLambdaCFRole"
        ))
        self.S3WriterCFCustomResource = self.add_resource(Function(
            "S3WriterCustomResource",
            Description=(
                "Creates, updates, and deletes S3 Files with custom content"
            ),
            Runtime="python2.7",
            Timeout=300,
            Handler="lambda_function.lambda_handler",
            Role=GetAtt(self.S3WriterLambdaCFExecRole, "Arn"),
            Code=Code(
                S3Bucket="cloudformation-custom-resources",
                S3Key="s3writer_lambda.zip",
            ),
            DependsOn="S3WriterCFPolicy"
        ))

    def _add_firehose(self):
        self.FirehoseLoggingBucket = self.add_resource(Bucket(
            "FirehoseLoggingBucket",
            DeletionPolicy="Retain",
        ))
        self.FirehoseLoggingRole = self.add_resource(Role(
            "FirehoseRole",
            AssumeRolePolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[AssumeRole],
                        Principal=Principal(
                            "Service", "firehose.amazonaws.com"
                        )
                    )
                ]
            ),
            Path="/",
        ))
        self.FirehosePolicy = self.add_resource(PolicyType(
            "FirehosePolicy",
            PolicyName="FirehoseRole",
            PolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[
                            s3.AbortMultipartUpload,
                            s3.GetBucketLocation,
                            s3.GetObject,
                            s3.ListBucket,
                            s3.ListBucketMultipartUploads,
                            s3.PutObject
                        ],
                        Resource=[
                            Join("", [
                                "arn:aws:s3:::",
                                Ref(self.FirehoseLoggingBucket),
                            ]),
                            Join("", [
                                "arn:aws:s3:::",
                                Ref(self.FirehoseLoggingBucket),
                                "/*",
                            ])
                        ]
                    ),
                ]
            ),
            Roles=[Ref(self.FirehoseLoggingRole)]
        ))
        self.FirehoseLogstream = self.add_resource(CustomResource(
            "FirehoseLogStream",
            ServiceToken=GetAtt(self.FirehoseCFCustomResource, "Arn"),
            S3DestinationConfiguration=dict(
                RoleARN=GetAtt(self.FirehoseLoggingRole, "Arn"),
                BucketARN=Join("", [
                    "arn:aws:s3:::",
                    Ref(self.FirehoseLoggingBucket),
                ]),
                BufferingHints=dict(
                    SizeInMBs=5,
                    IntervalInSeconds=60,
                )
            ),
            DependsOn=[
                "FirehosePolicy"
            ]
        ))
        self._template.add_output([
            Output(
                "FirehoseLoggingBucket",
                Description="Firehose Logging Bucket",
                Value=Ref(self.FirehoseLoggingBucket),
            )
        ])

    def _add_processor_databases(self):
        # Add the security group for Redis access
        self.LambdaProcessorSG = self.add_resource(SecurityGroup(
            "LambdaProcessorSG",
            GroupDescription="Lambda Message Processor",
            VpcId=Ref(self.ProcessorVPCId),
        ))
        self.RedisClusterSG = self.add_resource(SecurityGroup(
            "RedisClusterSG",
            SecurityGroupIngress=[
                SecurityGroupRule(
                    IpProtocol="tcp",
                    FromPort=6379,
                    ToPort=6379,
                    SourceSecurityGroupId=GetAtt(self.LambdaProcessorSG,
                                                 "GroupId")
                )
            ],
            GroupDescription="Allow HTTP traffic to redis",
            VpcId=Ref(self.ProcessorVPCId),
        ))
        self.RedisClusterSubnetGroup = self.add_resource(SubnetGroup(
            "RedisClusterSubnetGroup",
            Description="Subnet group for Redis Cluster",
            SubnetIds=[Ref(self.MessageAPISubnetId)],
        ))
        self.RedisCluster = self.add_resource(CacheCluster(
            "RedisPushMessages",
            Engine="redis",
            CacheNodeType="cache.m3.medium",
            NumCacheNodes=1,
            CacheSubnetGroupName=Ref(self.RedisClusterSubnetGroup),
            VpcSecurityGroupIds=[
                GetAtt(self.RedisClusterSG, "GroupId"),
            ],
            Tags=self._instance_tags("push-messages", "push-messages"),
        ))

    def _add_processor(self):
        self.ProcessorExecRole = self.add_resource(Role(
            "ProcessorExecRole",
            AssumeRolePolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[AssumeRole],
                        Principal=Principal(
                            "Service", "lambda.amazonaws.com"
                        )
                    )
                ]
            ),
            Path="/",
        ))

        # Common statements for accessing Redis
        self.PushMessageStatements = [
            Statement(
                Effect=Allow,
                Action=[
                    elasticache.DescribeCacheClusters,
                ],
                Resource=["*"]
            ),
            Statement(
                Effect=Allow,
                Action=[
                    ddb.ListTables,
                    ddb.DescribeTable,
                ],
                Resource=["*"]
            ),
        ]
        self.ProcessorLambdaPolicy = self.add_resource(PolicyType(
            "ProcessorLambdaPolicy",
            PolicyName="ProcessorLambdaRole",
            PolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[
                            Action("logs", "CreateLogGroup"),
                            Action("logs", "CreateLogStream"),
                            Action("logs", "PutLogEvents"),
                        ],
                        Resource=[
                            "arn:aws:logs:*:*:*"
                        ]
                    ),
                    Statement(
                        Effect=Allow,
                        Action=[
                            s3.GetBucketLocation,
                            s3.GetObject,
                            s3.ListBucket,
                            s3.ListBucketMultipartUploads,
                        ],
                        Resource=[
                            Join("", [
                                "arn:aws:s3:::",
                                Ref(self.FirehoseLoggingBucket),
                            ]),
                            Join("", [
                                "arn:aws:s3:::",
                                Ref(self.FirehoseLoggingBucket),
                                "/*",
                            ])
                        ]
                    ),
                    Statement(
                        Effect=Allow,
                        Action=[
                            ec2.CreateNetworkInterface,
                            ec2.DescribeNetworkInterfaces,
                            ec2.DeleteNetworkInterface,
                        ],
                        Resource=["*"]
                    ),
                ] + self.PushMessageStatements
            ),
            Roles=[Ref(self.ProcessorExecRole)],
            DependsOn="ProcessorExecRole"
        ))
        self.ProcessorS3Settings = self.add_resource(CustomResource(
            "ProcessorS3Settings",
            ServiceToken=GetAtt(self.S3WriterCFCustomResource, "Arn"),
            Bucket=Ref(self.FirehoseLoggingBucket),
            Key="processor_settings.json",
            Content=dict(
                redis_name=Ref(self.RedisCluster),
                file_type="json",
            ),
            DependsOn=[
                "S3WriterCustomResource"
            ]
        ))
        self.ProcessorLambda = self.add_resource(Function(
            "ProcessorLambda",
            Description=(
                "Processes logfiles when they hit S3"
            ),
            Runtime="python2.7",
            Timeout=300,
            Handler="lambda.handler",
            Role=GetAtt(self.ProcessorExecRole, "Arn"),
            Code=Code(
                S3Bucket=Ref(self.ProcessorLambdaBucket),
                S3Key=Ref(self.ProcessorLambdaKey),
            ),
            VpcConfig=VPCConfig(
                SecurityGroupIds=[
                    Ref(self.LambdaProcessorSG),
                ],
                SubnetIds=Ref(self.ProcessorSubnetIds),
            ),
            DependsOn=[
                "ProcessorExecRole",
                "ProcessorS3Settings",
            ]
        ))

    def _add_push_messages_api(self):
        self.MessagesServerRole = self.add_resource(Role(
            "MessagesServerRole",
            AssumeRolePolicyDocument=Policy(
                Version="2012-10-17",
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[AssumeRole],
                        Principal=Principal("Service", "ec2.amazonaws.com")
                    )
                ]
            ),
            Path="/",
        ))
        self.MessagesServerPolicy = self.add_resource(PolicyType(
            "MessagesServerPolicy",
            PolicyName="MessagesServerRole",
            PolicyDocument=Policy(
                Version="2012-10-17",
                Statement=self.PushMessageStatements,
            ),
            Roles=[Ref(self.MessagesServerRole)],
            DependsOn="MessagesServerRole"
        ))
        self.MessagesServerInstanceProfile = self.add_resource(InstanceProfile(
            "MessagesServerInstanceProfile",
            Path="/",
            Roles=[Ref(self.MessagesServerRole)]
        ))
        self.MessagesServerSG = self.add_resource(SecurityGroup(
            "MessagesServerSG",
            SecurityGroupIngress=[
                allow_tcp(22),
                allow_tcp(80),
            ],
            VpcId=Ref(self.ProcessorVPCId),
            GroupDescription="Allow HTTP traffic to Message Server node",
        ))
        self.MessagesServerInstance = self.add_resource(Instance(
            "MessagesServerInstance",
            ImageId="ami-2c393546",
            InstanceType="t2.micro",
            SecurityGroupIds=[
                GetAtt(self.LambdaProcessorSG, "GroupId"),
                GetAtt(self.MessagesServerSG, "GroupId"),
            ],
            KeyName=Ref(self.KeyPair),
            IamInstanceProfile=Ref(self.MessagesServerInstanceProfile),
            CreationPolicy=CreationPolicy(
                ResourceSignal=ResourceSignal(
                    Timeout='PT15M'
                )
            ),
            UserData=Base64(Join("", [
                "#cloud-config\n\n",
                "coreos:\n",
                "  units:\n",
                ] + self._aws_cfn_signal_service(
                    "pushmessages", "MessagesServerInstance") + [
                "    - name: 'pushmessages.service'\n",
                "      command: 'start'\n",
                "      content: |\n",
                "        [Unit]\n",
                "        Description=Push Messages container\n",
                "        Author=Mozilla Services\n",
                "        After=docker.service\n",
                "        \n",
                "        [Service]\n",
                "        Restart=always\n",
                "        ExecStartPre=-/usr/bin/docker kill pushmessages\n",
                "        ExecStartPre=-/usr/bin/docker rm pushmessages\n",
                "        ExecStartPre=/usr/bin/docker pull ",
                "bbangert/push-messages:", Ref(self.PushMessagesVersion), "\n",
                "        ExecStart=/usr/bin/docker run ",
                "--name pushmessages ",
                "-p 80:8000 ",
                "-e 'AWS_DEFAULT_REGION=us-east-1' ",
                "-e 'REDIS_ELASTICACHE=", Ref(self.RedisCluster), "' ",
                "bbangert/push-messages:", Ref(self.PushMessagesVersion), "\n",
            ])),
            SubnetId=Ref(self.MessageAPISubnetId),
            DependsOn="MessagesServerPolicy",
            Tags=self._instance_tags("push-messages", "push-messages"),
        ))
        self._template.add_output([
            Output(
                "MessagesAPI",
                Description="Push Messages API URL",
                Value=Join("", [
                    "http://",
                    GetAtt(self.MessagesServerInstance, "PublicIp"),
                    "/"
                ])
            )
        ])

    def json(self):
        return self._template.to_json()


if __name__ == '__main__':
    cli()
