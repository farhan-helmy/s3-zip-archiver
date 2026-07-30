# AWS-maintained Lambda base image. It already contains the Python runtime, the
# Lambda Runtime Interface Client and boto3, so nothing else is needed here.
#
# The handler deliberately uses only the standard library (zipfile) plus boto3.
# There is no requirements.txt and no pip layer, which keeps the image small, the
# build fast, and the third-party supply-chain surface at zero.
FROM public.ecr.aws/lambda/python:3.12

# Pinned to the minor version rather than a digest. Rollback safety does not
# depend on it: each deploy pushes an immutable, SHA-tagged image to ECR, and a
# Lambda version points at that exact image - so rolling back always resolves to
# the same bytes regardless of what :3.12 points to today. Leaving the tag
# floating means rebuilds pick up AWS's base-image security patches.

COPY app.py ${LAMBDA_TASK_ROOT}/

CMD ["app.lambda_handler"]
