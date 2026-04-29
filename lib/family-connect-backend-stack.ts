import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { WebSocketLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as iam from 'aws-cdk-lib/aws-iam'; 
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';

export class FamilyConnectBackendStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Connection management table (Stores active WebSocket connection IDs)
    const connectionsTable = new dynamodb.Table(this, 'ConnectionsTable', {
      tableName: 'Connections',
      partitionKey: { name: 'connectionId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Chat history table (Partitioned by roomId, sorted by timestamp for chronological retrieval)
    const chatHistoryTable = new dynamodb.Table(this, 'ChatHistoryTable', {
      tableName: 'ChatHistory',
      partitionKey: { name: 'roomId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING }, 
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Core WebSocket message handler
    const connectLambda = new lambda.Function(this, 'ConnectHandlerLambda', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'connect.lambda_handler',
      code: lambda.Code.fromAsset('lambda'),
      environment: {
        TABLE_NAME: connectionsTable.tableName,
        HISTORY_TABLE_NAME: chatHistoryTable.tableName,
      }
    });

    connectionsTable.grantReadWriteData(connectLambda);
    chatHistoryTable.grantReadWriteData(connectLambda); 

    // WebSocket API configuration
    const webSocketApi = new apigwv2.WebSocketApi(this, 'FamilyConnectApi', {
      apiName: 'FamilyConnectChat',
    });

    // Route integrations
    webSocketApi.addRoute('$connect', {
      integration: new WebSocketLambdaIntegration('ConnectIntegration', connectLambda)
    });
    webSocketApi.addRoute('$disconnect', {
      integration: new WebSocketLambdaIntegration('DisconnectIntegration', connectLambda)
    });
    webSocketApi.addRoute('$default', {
      integration: new WebSocketLambdaIntegration('DefaultIntegration', connectLambda)
    });

    // API Gateway stage setup
    const apiStage = new apigwv2.WebSocketStage(this, 'DevStage', {
      webSocketApi,
      stageName: 'dev',
      autoDeploy: true,
    });

    // Allow Lambda to push messages back to connected clients via API Gateway
    connectLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['execute-api:ManageConnections'],
      resources: ['arn:aws:execute-api:*:*:*/*'],
    }));

    // Output: WebSocket endpoint URL
    new cdk.CfnOutput(this, 'WebSocketURL', {
      value: apiStage.url,
      description: 'The WSS URL to connect to the API Gateway',
    });

    // Frontend hosting bucket
    const websiteBucket = new s3.Bucket(this, 'FamilyConnectWebsiteBucket', {
      websiteIndexDocument: 'index.html',
      publicReadAccess: true,
      // Explicitly disable BlockPublicAccess to allow website hosting
      blockPublicAccess: new s3.BlockPublicAccess({
        blockPublicAcls: false,
        blockPublicPolicy: false,
        ignorePublicAcls: false,
        restrictPublicBuckets: false,
      }),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // Deploy frontend assets to S3
    new s3deploy.BucketDeployment(this, 'DeployWebsite', {
      sources: [s3deploy.Source.asset('./frontend')],
      destinationBucket: websiteBucket,
    });

    // CI/CD OIDC Provider for GitHub Actions
    const githubProvider = new iam.OpenIdConnectProvider(this, 'GitHubProvider', {
      url: 'https://token.actions.githubusercontent.com',
      clientIds: ['sts.amazonaws.com'],
    });

    // Role for GitHub Actions to deploy CDK via OIDC
    const deployRole = new iam.Role(this, 'GitHubDeployRole', {
      assumedBy: new iam.FederatedPrincipal(
        githubProvider.openIdConnectProviderArn,
        {
          StringLike: {
            'token.actions.githubusercontent.com:sub': 'repo:atsuo-architect/family-connect:*',
          },
          StringEquals: {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
          },
        },
        'sts:AssumeRoleWithWebIdentity',
      ),
      description: 'Role for GitHub Actions to deploy CDK',
    });
    
    // Grant deployment permissions
    deployRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AdministratorAccess'));

    // Output: Role ARN to be used in GitHub Actions Secrets
    new cdk.CfnOutput(this, 'DeployRoleArn', { value: deployRole.roleArn });
    
    // Output: Frontend website URL
    new cdk.CfnOutput(this, 'WebsiteURL', {
      value: websiteBucket.bucketWebsiteUrl,
      description: 'The URL of the chat application',
    });
  }
}