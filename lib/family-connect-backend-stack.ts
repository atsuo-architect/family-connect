import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { WebSocketLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as apigwv2_authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';

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
    // webSocketApi.addRoute('$connect', {
    //   integration: new WebSocketLambdaIntegration('ConnectIntegration', connectLambda)
    // });
    webSocketApi.addRoute('$disconnect', {
      integration: new WebSocketLambdaIntegration('DisconnectIntegration', connectLambda)
    });
    webSocketApi.addRoute('$default', {
      integration: new WebSocketLambdaIntegration('DefaultIntegration', connectLambda)
    });
    webSocketApi.addRoute('getHistory', {
      integration: new WebSocketLambdaIntegration('HistoryIntegration', connectLambda)
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
      // websiteIndexDocument: 'index.html',
      // publicReadAccess: true,
      // OACを使うので、パブリックアクセスはすべてブロック
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const distribution = new cloudfront.Distribution(this, 'WebsiteDistribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(websiteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      }
    });

    new s3deploy.BucketDeployment(this, 'DeployWebsite', {
      sources: [s3deploy.Source.asset('./frontend')],
      destinationBucket: websiteBucket,
      distribution,
      distributionPaths: ['/*'],
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

    // --- 11. Cognito ユーザープールの作成 ---
    const userPool = new cognito.UserPool(this, 'FamilyConnectUserPool', {
      userPoolName: 'FamilyConnectUsers',
      selfSignUpEnabled: false, // 家族以外が勝手に登録できないようにする
      signInAliases: { username: true, email: true }, // メールアドレスでもログイン可能
      autoVerify: { email: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY, // ポートフォリオ用なので削除可能に
    });

    // アプリケーションクライアント（フロントエンドから接続するための窓口）
    const userPoolClient = userPool.addClient('FamilyConnectAppClient', {
      authFlows: {
        userPassword: true, // ID/PASS でのログインを許可
      },
    });

    // --- 12. Lambda オーソライザー（認証用 Lambda） ---
    const authLambda = new lambda.Function(this, 'WebSocketAuthHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'auth.lambda_handler',
      code: lambda.Code.fromAsset('lambda'),
      environment: {
        USER_POOL_ID: userPool.userPoolId,
        APP_CLIENT_ID: userPoolClient.userPoolClientId,
      },
    });

    // API Gateway に「この Lambda で鍵をチェックしろ」と指示する
    const authorizer = new apigwv2_authorizers.WebSocketLambdaAuthorizer('ChatAuthorizer', authLambda, {
      identitySource: ['route.request.querystring.token'], // URLの末尾に ?token=xxx で渡すルール
    });

    // --- 13. WebSocket API の $connect にオーソライザーを適用 ---
    // ※既存の webSocketApi.addRoute('$connect', ...) を以下のように書き換えます
    webSocketApi.addRoute('$connect', {
      integration: new WebSocketLambdaIntegration('ConnectIntegration', connectLambda),
      authorizer: authorizer, // 接続時に認証を強制
    });

    // 出力追加
    new cdk.CfnOutput(this, 'UserPoolId', { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'ClientId', { value: userPoolClient.userPoolClientId });

    // Grant deployment permissions
    deployRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AdministratorAccess'));

    // Output: Role ARN to be used in GitHub Actions Secrets
    new cdk.CfnOutput(this, 'DeployRoleArn', { value: deployRole.roleArn });

    // Output: Frontend website URL
    new cdk.CfnOutput(this, 'CloudFrontURL', {
      value: `https://${distribution.distributionDomainName}`, 
      description: 'The URL of the CloudFront distribution',
    });
  }
}