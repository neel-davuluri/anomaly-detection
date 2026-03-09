terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  required_version = ">= 1.3.0"
}

provider "aws" {
  region = "us-east-1"
}

# ── Variables ─────────────────────────────────────────────────────────────────

variable "bucket_name" {
  type        = string
  default     = "ds5220-dp1-xbk9fh"
  description = "Globally unique S3 bucket name — append your UVA ID to make it unique"
}

variable "github_repo" {
  type        = string
  default     = "https://github.com/neel-davuluri/anomaly-detection.git"
  description = "URL of your forked anomaly-detection repo"
}

variable "ssh_key_name" {
  type        = string
  description = "Name of an existing EC2 key pair for SSH access"
}

variable "my_ip" {
  type        = string
  default     = "104.145.79.42/32"
  description = "Your IP address for SSH access (CIDR notation)"
}

# ── IAM Role ──────────────────────────────────────────────────────────────────

resource "aws_iam_role" "ec2_role" {
  name = "ds5220-dp1-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "s3_policy" {
  name = "ds5220-dp1-s3-policy"
  role = aws_iam_role.ec2_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ]
      Resource = [
        "arn:aws:s3:::${var.bucket_name}",
        "arn:aws:s3:::${var.bucket_name}/*"
      ]
    }]
  })
}

resource "aws_iam_instance_profile" "ec2_profile" {
  name = "ds5220-dp1-instance-profile"
  role = aws_iam_role.ec2_role.name
}

# ── Security Group ────────────────────────────────────────────────────────────

resource "aws_security_group" "anomaly_sg" {
  name        = "ds5220-dp1-sg"
  description = "Allow SSH from my IP and port 8000 from anywhere"

  ingress {
    description = "SSH from my IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip]
  }

  ingress {
    description = "FastAPI from anywhere"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ── EC2 Instance ──────────────────────────────────────────────────────────────

resource "aws_instance" "anomaly_instance" {
  # Ubuntu 24.04 LTS (us-east-1) — verify current AMI ID before deploying
  ami                    = "ami-084568db4383264d4"
  instance_type          = "t3.micro"
  key_name               = var.ssh_key_name
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name
  vpc_security_group_ids = [aws_security_group.anomaly_sg.id]

  root_block_device {
    volume_size = 16
    volume_type = "gp3"
  }

  user_data = <<-EOF
    #!/bin/bash
    set -e
    exec > /var/log/userdata.log 2>&1

    apt-get update -y
    apt-get install -y git python3 python3-pip python3-venv

    echo 'BUCKET_NAME="${var.bucket_name}"' >> /etc/environment
    export BUCKET_NAME="${var.bucket_name}"

    git clone ${var.github_repo} /opt/anomaly-detection

    python3 -m venv /opt/anomaly-detection/venv
    /opt/anomaly-detection/venv/bin/pip install --upgrade pip
    /opt/anomaly-detection/venv/bin/pip install -r /opt/anomaly-detection/requirements.txt

    touch /var/log/anomaly-detection.log
    chmod 666 /var/log/anomaly-detection.log

    cat > /etc/systemd/system/anomaly-detection.service << 'SVCEOF'
    [Unit]
    Description=Anomaly Detection FastAPI Service
    After=network.target

    [Service]
    User=root
    WorkingDirectory=/opt/anomaly-detection
    EnvironmentFile=/etc/environment
    ExecStart=/opt/anomaly-detection/venv/bin/fastapi run /opt/anomaly-detection/app.py --host 0.0.0.0 --port 8000
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=multi-user.target
    SVCEOF

    systemctl daemon-reload
    systemctl enable anomaly-detection
    systemctl start anomaly-detection
  EOF

  tags = {
    Name = "ds5220-dp1-anomaly-instance"
  }
}

# ── Elastic IP ────────────────────────────────────────────────────────────────

resource "aws_eip" "anomaly_eip" {
  domain = "vpc"
}

resource "aws_eip_association" "anomaly_eip_assoc" {
  instance_id   = aws_instance.anomaly_instance.id
  allocation_id = aws_eip.anomaly_eip.id
}

# ── SNS Topic ─────────────────────────────────────────────────────────────────

resource "aws_sns_topic" "anomaly_topic" {
  name = "ds5220-dp1"
}

# ── SNS Topic Policy — allows S3 to publish ───────────────────────────────────

resource "aws_sns_topic_policy" "allow_s3" {
  arn = aws_sns_topic.anomaly_topic.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowS3ToPublish"
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.anomaly_topic.arn
      Condition = {
        ArnLike = {
          "aws:SourceArn" = "arn:aws:s3:::${var.bucket_name}"
        }
      }
    }]
  })
}

# ── SNS Subscription → EC2 /notify ───────────────────────────────────────────

resource "aws_sns_topic_subscription" "http_endpoint" {
  topic_arn              = aws_sns_topic.anomaly_topic.arn
  protocol               = "http"
  endpoint               = "http://${aws_eip.anomaly_eip.public_ip}:8000/notify"
  endpoint_auto_confirms = true   # FastAPI /notify handles SubscriptionConfirmation automatically

  depends_on = [aws_eip_association.anomaly_eip_assoc]
}

# ── S3 Bucket (created after SNS policy so notification config works) ─────────

resource "aws_s3_bucket" "anomaly_bucket" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_notification" "s3_to_sns" {
  bucket = aws_s3_bucket.anomaly_bucket.id

  topic {
    topic_arn     = aws_sns_topic.anomaly_topic.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "raw/"
    filter_suffix = ".csv"
  }

  depends_on = [aws_sns_topic_policy.allow_s3]
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "instance_public_ip" {
  description = "Elastic IP of the EC2 instance"
  value       = aws_eip.anomaly_eip.public_ip
}

output "api_endpoint" {
  description = "Base URL for the FastAPI service"
  value       = "http://${aws_eip.anomaly_eip.public_ip}:8000"
}

output "health_check_url" {
  description = "Health check endpoint"
  value       = "http://${aws_eip.anomaly_eip.public_ip}:8000/health"
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket"
  value       = aws_s3_bucket.anomaly_bucket.id
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic"
  value       = aws_sns_topic.anomaly_topic.arn
}
