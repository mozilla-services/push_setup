import uuid

import awacs.dynamodb as ddb
import click
from awacs.aws import (
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
    Template,
)
from troposphere.ec2 import (
    Instance,
    SecurityGroup,
    SecurityGroupRule
)
from troposphere.iam import (
    InstanceProfile,
    PolicyType,
    Role,
)


@click.group()
def cli():
    pass


@click.command()
def push():
    cb = CloudFormationBuilder()
    print cb.json()


cli.add_command(push)


# Common bits
def allow_tcp(port, from_ip="0.0.0.0/0"):
    return SecurityGroupRule(
        IpProtocol="tcp",
        FromPort=port,
        ToPort=port,
        CidrIp=from_ip
    )


class CloudFormationBuilder(object):
    def __init__(self):
        self._random_id = str(uuid.uuid4()).replace('-', '')[:12].upper()
        self._template = Template()
        self._template.add_version("2010-09-09")
        self.add_resource = self._template.add_resource
        self.add_parameter = self._template.add_parameter

        self.AutopushVersion = self.add_parameter(Parameter(
            "AutopushVersion",
            Type="String",
            Description="Autopush version to deploy",
            Default="1.14.1",
            AllowedValues=[
                "1.14.1",
            ]
        ))
        self.PushVPC = self.add_parameter(Parameter(
            "ExistingVPC",
            Type="AWS::EC2::VPC::Id",
            Description=(
                "The VPC Id so launch the Autopush server into"
                ),
        ))
        self.PushCryptoKey = self.add_parameter(Parameter(
            "AutopushCryptoKey",
            Type="String",
            Default=Fernet.generate_key(),
            Description="Autopush crypto-key",
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

        self._add_autopush_security_group()
        self._add_autopush_iam_roles()
        self._add_autopush_servers()

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
                ]
            ),
            Roles=[Ref(self.PushServerRole)]
        ))

    def _add_autopush_servers(self):
        self.PushServerInstanceProfile = self.add_resource(InstanceProfile(
            "AutopushServerInstanceProfile",
            Path="/",
            Roles=[Ref(self.PushServerRole)]
        ))
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
            UserData=Base64(Join("", [
                "#cloud-config\n\n",
                "coreos:\n",
                "  units:\n",
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
                "bbangert/autopush:", Ref(self.AutopushVersion), " ",
                "./pypy/bin/autoendpoint ",
                "--hostname $public_ipv4 ",
                "--storage_tablename ", Ref(self.PushTablePrefix),
                "_storage ",
                "--message_tablename ", Ref(self.PushTablePrefix),
                "_message ",
                "--router_tablename ", Ref(self.PushTablePrefix),
                "_router ",
                "--crypto_key '", Ref(self.PushCryptoKey), "' ",
                "\n"
            ]))
        ))
        self.PushConnectionServerInstance = self.add_resource(Instance(
            "AutopushConectionInstance",
            ImageId="ami-2c393546",
            InstanceType="t2.micro",
            SecurityGroups=[
                Ref(self.ConnectionSG),
                Ref(self.InternalRouterSG),
            ],
            KeyName=Ref(self.KeyPair),
            IamInstanceProfile=Ref(self.PushServerInstanceProfile),
            UserData=Base64(Join("", [
                "#cloud-config\n\n",
                "coreos:\n",
                "  units:\n",
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
                "bbangert/autopush:", Ref(self.AutopushVersion), " ",
                "./pypy/bin/autopush ",
                "--hostname $public_ipv4 ",
                "--storage_tablename ", Ref(self.PushTablePrefix),
                "_storage ",
                "--message_tablename ", Ref(self.PushTablePrefix),
                "_message ",
                "--router_tablename ", Ref(self.PushTablePrefix),
                "_router ",
                "--router_hostname $private_ipv4 ",
                "--endpoint_hostname ",
                GetAtt(self.PushEndpointServerInstance, "PublicDnsName"),
                " ",
                "--crypto_key '", Ref(self.PushCryptoKey), "' ",
                "\n"
            ]))
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

    def json(self):
        return self._template.to_json()


if __name__ == '__main__':
    cli()
