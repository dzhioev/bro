# bro-oops-cdk

`bro-oops-cdk` is the AWS CDK construct library for the platform and services `bro-oops` deploys.
Install it in the environment a CDK app synthesizes in;
`bro-oops` itself, with its persona, `infra` toolset, and deployment configuration, runs without the CDK.

## Constructs

Every stack reads its names from the typed configuration `bro.oops.config.resolve()` returns (`../README.md`, "Deployment configuration").

`PlatformStack` creates the shared VPC, ECS cluster, ALB, HTTPS listener, wildcard certificate, and Route 53 lookup.
One app deploys it;
every app carrying a service on that platform finds it instead, so a service and its platform need not live in the same CDK app.
Tests can pass `HostedZoneReference` to synthesize without an AWS context lookup.

Service stacks take the platform's VPC, cluster, hosted zone, load balancer, and HTTPS listener as a `PlatformHandles` value.
`PlatformHandles.lookup()` finds the VPC and ALB by their CloudFormation stack tag, the ECS cluster by its configured name, the hosted zone by domain, and the HTTPS listener by port.
A service assertion test can inject `PlatformStack.handles` or a `PlatformHandles` fixture instead of resolving those lookups.

`RepositoryStack` creates one configured ECR repository while preserving its configured construct id.
`ImageBuildStack` creates the configured CodeBuild project, grants it pushes to every configured repository, and reads the checkout-relative buildspec and image-build script paths from the same credential.
A private source needs a GitHub identity:
`connection_name` has the stack create a connection, `connection_arn` points the project at one that already exists, and neither leaves a public source unauthenticated.
The project names its connection through the source's `Auth` block rather than through a CodeBuild source credential, which is a single default per account and region and so cannot serve two image-build stacks.

`TrailsServerStack` owns the retained DynamoDB tables and S3 spillover bucket, the store-config parameter, the Fargate service, ALB rule, and DNS record.
Its DynamoDB table, key, and index declarations come from `bro.trails.server.dynamo`.
