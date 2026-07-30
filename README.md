# s3-zip-archiver

Serverless pipeline that compresses objects landing in an S3 bucket into ZIP archives,
writes them back to the same bucket, and deletes the originals once the upload is verified.

Built with AWS SAM. The Lambda runs as a container image inside private subnets of a
purpose-built VPC, reaching S3 through a free S3 Gateway VPC Endpoint.

Full documentation follows in later commits.
