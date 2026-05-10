# Safety Guardrails — Dangerous Operations Requiring Confirmation

> **CRITICAL PROTOCOL**: The following commands MUST NEVER be executed without explicit user confirmation. These operations can cause irreversible data loss, security breaches, or system damage.

---

## 🔴 DATABASE DESTRUCTIVE OPERATIONS

### NEVER EXECUTE WITHOUT CONFIRMATION:

```sql
-- DDL - Data Definition Language (Destructive)
DROP TABLE <table_name>;                    -- Deletes table and all data
DROP DATABASE <database_name>;              -- Deletes entire database
DROP SCHEMA <schema_name>;                  -- Deletes schema with all objects
DROP INDEX <index_name>;                    -- Deletes index
DROP VIEW <view_name>;                      -- Deletes view
DROP FUNCTION <function_name>;              -- Deletes function
DROP TRIGGER <trigger_name>;                -- Deletes trigger
DROP SEQUENCE <sequence_name>;              -- Deletes sequence

-- DML - Data Destruction
TRUNCATE TABLE <table_name>;               -- Deletes ALL rows instantly (no rollback)
DELETE FROM <table_name>;                  -- Without WHERE clause = all data gone

-- ALTER - Destructive modifications
ALTER TABLE <table> DROP COLUMN <col>;     -- Permanent column deletion
ALTER TABLE <table> DROP CONSTRAINT <con>; -- Removes constraints

-- Supabase/PostgreSQL specific
DROP POLICY <policy_name> ON <table>;       -- Removes RLS policy
DROP PUBLICATION <name>;                    -- Removes replication
DROP SUBSCRIPTION <name>;                   -- Removes subscription
REASSIGN OWNED BY <old_user> TO <new_user>; -- Mass ownership change
```

### ⚠️ MODIFICATIONS REQUIRING CONFIRMATION:

```sql
-- Schema changes that could break application
ALTER TABLE <table> ALTER COLUMN <col> TYPE <new_type>;
ALTER TABLE <table> RENAME TO <new_name>;
ALTER TABLE <table> RENAME COLUMN <old> TO <new>;

-- Index operations (can cause downtime)
DROP INDEX CONCURRENTLY <index_name>;
REINDEX TABLE <table_name>;                -- Locks table

-- Migration operations
ALTER TABLE <table> SET SCHEMA <new_schema>;
```

---

## 🔴 FILE SYSTEM DESTRUCTIVE OPERATIONS

### NEVER EXECUTE WITHOUT CONFIRMATION:

```bash
# Recursive deletion (RM)
rm -rf /                                    # Deletes EVERYTHING (system killer)
rm -rf /*                                   # Deletes all root contents
rm -rf ~                                    # Deletes home directory
rm -rf .                                    # Deletes current directory
rm -rf ..                                   # Deletes parent directory
rm -rf /home                                # Deletes all user data
rm -rf /var                                 # Deletes logs and data
rm -rf /etc                                 # Deletes configuration
rm -rf /usr                                 # Deletes system binaries
rm -rf <path>/*                           # Deletes all contents
find <path> -type f -delete               # Mass file deletion
find <path> -type d -delete               # Mass directory deletion

# Disk operations
mkfs.ext4 /dev/sda                        # Formats entire disk
mkfs.ntfs /dev/sda1                       # Formats partition
fdisk /dev/sda                            # Disk partitioning (can wipe)
dd if=/dev/zero of=/dev/sda               # Wipes disk completely
parted /dev/sda rm <number>               # Removes partition
pvremove /dev/sda                         # Removes LVM physical volume

# Mass operations
rm -f *.log *.txt *.json                  # Mass deletion by pattern
rm -f *                                   # Delete all in current dir
delete * /f /s /q                         # Windows recursive force delete
rd /s /q <folder>                         # Windows remove directory
del /f /s /q <pattern>                    # Windows force delete

# Move/rename disasters
mv <file> /dev/null                       # Destroys file content
mv /important/path /tmp                   # Moves to temp (may be auto-deleted)
mv <file> <file>                          # Can corrupt if interrupted

# Empty/truncate
truncate -s 0 <important_file>            # Zeroes file content
> <important_file>                        # Empties file content
: > <important_file>                      # Empties file content
cat /dev/null > <file>                    # Empties file content
```

### ⚠️ RISKY OPERATIONS:

```bash
# Operations that modify many files
chmod -R 777 /                            # Makes everything writable (security risk)
chown -R root:root /home                  # Changes ownership en masse
chmod -R 000 <path>                       # Removes all permissions

# Symlink operations (can point to sensitive locations)
ln -sf /etc/passwd <target>               # Can overwrite system files
rm -rf <symlink>/                         # May follow symlink and delete target

# Archive extraction
tar -xzf archive.tar.gz -C /              # Extract to root (can overwrite)
unzip archive.zip -d /                    # Extract to root
```

---

## 🔴 GIT DESTRUCTIVE OPERATIONS

### NEVER EXECUTE WITHOUT CONFIRMATION:

```bash
# History destruction
git reset --hard HEAD~N                   # Destroys commits permanently
git reset --hard <commit>                 # Moves HEAD, destroys changes
git reset --hard origin/main              # Destroys local changes
git clean -fd                             # Deletes untracked files
git clean -fdx                            # Deletes untracked + ignored files

# Branch operations
git branch -D <branch>                    # Force deletes branch
git push origin --delete <branch>         # Deletes remote branch
git push origin :<branch>                 # Deletes remote branch (old syntax)

# History rewriting
git rebase -i --root                     # Rewrites entire history
git filter-branch --force                 # Rewrites history
git filter-repo                           # Rewrites history (newer)
git commit --amend --reset-author         # Modifies last commit
git rebase --abort                        # Can lose work if used wrong

# Force push (DANGEROUS)
git push --force                          # Overwrites remote history
git push -f                               # Short form, equally dangerous
git push --force-with-lease               # Safer, but still risky
git push origin +branch                  # Force push syntax

# Reflog cleanup
git reflog expire --expire=now --all      # Deletes reflog (recovery info)
git gc --prune=now --aggressive           # Aggressive garbage collection

# Submodule destruction
git submodule deinit -f <path>            # Removes submodule
git rm -f <submodule>                     # Removes submodule files

# Worktree cleanup
git worktree remove -f <path>             # Force removes worktree
```

### ⚠️ HISTORY REWRITING (ALWAYS CONFIRM):

```bash
git rebase -i HEAD~N                      # Interactive rebase
git cherry-pick --skip                    # Skips commit
git cherry-pick --abort                   # Aborts cherry-pick
```

---

## 🔴 NETWORK & SECURITY DESTRUCTIVE OPERATIONS

### NEVER EXECUTE WITHOUT CONFIRMATION:

```bash
# Firewall operations
iptables -F                               # Flushes ALL rules (open everything)
iptables -X                               # Deletes all custom chains
iptables -P INPUT ACCEPT                  # Opens all inbound traffic
iptables -P OUTPUT ACCEPT                 # Opens all outbound traffic
iptables -P FORWARD ACCEPT                # Opens forwarding
ufw disable                               # Disables firewall
systemctl stop firewalld                  # Stops firewall

# Network interface operations
ifconfig eth0 down                        # Brings down network interface
ip link set eth0 down                     # Brings down network interface
ip addr flush dev eth0                    # Removes all IPs from interface

# SSL/Certificate operations
rm -rf /etc/ssl/                          # Deletes SSL certificates
rm -rf /etc/letsencrypt/                  # Deletes Let's Encrypt certs
certbot delete --cert-name <domain>       # Deletes certificate

# SSH operations
rm -rf ~/.ssh/                            # Deletes SSH keys
rm ~/.ssh/authorized_keys                 # Removes authorized keys
ssh-keygen -f ~/.ssh/id_rsa               # Overwrites existing key

# Access control
chmod 777 /etc/shadow                     # Exposes password hashes
chmod 644 /etc/shadow                     # Makes passwords readable
chmod 777 /etc/passwd                     # Exposes user list
```

---

## 🔴 SYSTEM & SERVICE DESTRUCTIVE OPERATIONS

### NEVER EXECUTE WITHOUT CONFIRMATION:

```bash
# Service management
systemctl stop <critical_service>         # Stops services (ssh, database, etc)
systemctl disable <critical_service>       # Disables auto-start
systemctl mask <service>                   # Completely disables service
service <name> stop                       # Stops service

# Process operations
kill -9 1                                 # Kills init (system crash)
pkill -9 <process>                        # Force kills processes
killall -9 <name>                         # Force kills by name
kill -9 -1                                # Kills all user processes
kill -9 0                                 # Kills process group

# User management
userdel -r <username>                     # Deletes user and home
groupdel <groupname>                        # Deletes group
usermod -L <username>                       # Locks user account
passwd -l <username>                        # Locks password

# Package management
apt purge <package>                         # Removes package + config
apt autoremove --purge                      # Aggressive cleanup
apt remove --purge <critical>               # Removes critical packages
yum remove <critical_package>             # Removes RPM package
pip uninstall -y <package>                  # Force uninstall
npm uninstall -g <package>                  # Global uninstall

# Environment cleanup
unset <CRITICAL_VAR>                        # Unsets important variables
rm -rf /etc/environment                     # Deletes environment config
rm -rf /etc/profile.d/                      # Deletes profile scripts

# Log operations
echo "" > /var/log/syslog                   # Empties system log
rm -rf /var/log/                            # Deletes all logs
> /var/log/messages                         # Truncates messages log
```

---

## 🔴 PYTHON/DJANGO/FLASK DESTRUCTIVE OPERATIONS

### NEVER EXECUTE WITHOUT CONFIRMATION:

```python
# Database operations
Model.objects.all().delete()               # Deletes ALL records
Model.objects.filter().delete()            # Mass deletion
User.objects.all().delete()                # Deletes all users

# Django management
python manage.py flush                     # Deletes all data
python manage.py migrate zero              # Reverses all migrations
python manage.py migrate <app> zero        # Reverses app migrations
python manage.py dbshell                   # Direct DB shell access
python manage.py shell                     # Production shell access

# Migration operations
python manage.py migrate --fake            # Fakes migrations (dangerous)
python manage.py migrate --fake-initial    # Fakes initial (dangerous)

# Cache operations
cache.clear()                              # Clears entire cache
redis_client.flushall()                    # Flushes all Redis databases
redis_client.flushdb()                     # Flushes current Redis DB

# File operations in Python
shutil.rmtree(path)                        # Recursive delete
os.system("rm -rf /")                      # System command
subprocess.run("rm", shell=True)           # Shell subprocess
pathlib.Path("/").rmdir()                  # Remove directory
os.remove("/etc/passwd")                   # Remove system file

# Environment
os.environ.clear()                         # Clears all env vars
del os.environ["CRITICAL_VAR"]             # Deletes important variable
```

---

## 🔴 DOCKER/KUBERNETES DESTRUCTIVE OPERATIONS

### NEVER EXECUTE WITHOUT CONFIRMATION:

```bash
# Docker
docker system prune -a                      # Removes all unused (volumes too!)
docker system prune -a --volumes            # Removes all + volumes
docker volume prune                         # Removes all unused volumes
docker image prune -a                       # Removes all images
docker container prune                      # Removes all stopped containers
docker network prune                        # Removes all networks
docker rmi -f $(docker images -q)          # Deletes ALL images
docker rm -f $(docker ps -aq)              # Deletes ALL containers
docker-compose down -v                     # Removes volumes too
docker-compose rm -fv                      # Force removes with volumes

# Kubernetes
kubectl delete all --all                    # Deletes everything in namespace
kubectl delete namespace <name>             # Deletes entire namespace
kubectl delete pods --all                   # Deletes all pods
kubectl delete deployments --all            # Deletes all deployments
kubectl delete services --all             # Deletes all services
kubectl delete pvc --all                    # Deletes persistent volumes
kubectl delete pv --all                     # Deletes volume claims
kubectl drain <node> --force --ignore-daemonsets --delete-local-data  # Drains node
curl -X DELETE <k8s-api-endpoint>           # Direct API deletion
```

---

## 🔴 CLOUD PROVIDER DESTRUCTIVE OPERATIONS

### NEVER EXECUTE WITHOUT CONFIRMATION:

```bash
# AWS CLI
aws s3 rm s3://bucket-name --recursive      # Deletes entire S3 bucket
aws s3 rb s3://bucket-name --force          # Removes bucket + contents
aws ec2 terminate-instances                 # Terminates EC2 instances
aws rds delete-db-instance                  # Deletes RDS database
aws rds delete-db-cluster                   # Deletes Aurora cluster
aws dynamodb delete-table                   # Deletes DynamoDB table
aws lambda delete-function                  # Deletes Lambda function
aws iam delete-user --user-name <name>      # Deletes IAM user
aws iam delete-role --role-name <name>      # Deletes IAM role
aws cloudformation delete-stack             # Deletes entire stack
aws sagemaker delete-notebook-instance      # Deletes SageMaker

# GCP CLI
gcloud sql instances delete                 # Deletes Cloud SQL
gcloud compute instances delete             # Deletes VM instances
gcloud storage rm -r gs://bucket            # Deletes GCS bucket
gcloud app services delete                  # Deletes App Engine
gcloud functions delete                     # Deletes Cloud Functions
gcloud container clusters delete            # Deletes GKE cluster

# Azure CLI
az storage blob delete-batch                # Mass blob deletion
az sql db delete                            # Deletes Azure SQL DB
az group delete --name <rg>                 # Deletes resource group
az vm delete                                # Deletes VM
az keyvault delete                          # Deletes Key Vault
az cosmosdb delete                          # Deletes Cosmos DB
```

---

## 🛡️ CONFIRMATION PROTOCOL

### BEFORE EXECUTING ANY DANGEROUS COMMAND:

1. **STOP** — Do not execute immediately
2. **VERIFY** — Confirm the command is necessary
3. **BACKUP** — Ensure backups exist
4. **ASK** — Get explicit user confirmation:
   ```
   "This command will [DESCRIPTION OF IMPACT]. 
    Are you sure you want to proceed? Type 'YES DELETE' to confirm."
   ```
5. **LOG** — Document what will be done
6. **EXECUTE** — Only after explicit confirmation

### CONFIRMATION MESSAGE TEMPLATE:

```
⚠️ DANGEROUS OPERATION DETECTED ⚠️

Command: [COMMAND]
Impact: [WHAT WILL HAPPEN]
Risk Level: 🔴 CRITICAL / 🟡 HIGH / 🟢 MEDIUM

Data at risk:
- [List of affected data/files/systems]

Backup status: [Yes/No/Unknown]

Type the following to confirm: "[UNIQUE_CODE]"
Or reply: "cancel" to abort.
```

---

## ⚠️ EXCEPTIONS (When Safe to Proceed)

These conditions MAY allow execution WITHOUT additional confirmation:

✅ **Test environments** — Explicitly marked with `TEST_ENV=true`  
✅ **Fresh installations** — No existing data at risk  
✅ **User explicitly requested** — User said "force delete all"  
✅ **Dry-run mode** — Command has `--dry-run` flag  
✅ **Rollback available** — Confirmed backup/snapshot exists  

---

## 📋 SAFETY CHECKLIST

Before any destructive operation:

- [ ] Is this a production environment?
- [ ] Is there existing data at risk?
- [ ] Is there a confirmed backup?
- [ ] Has user explicitly confirmed?
- [ ] Is the command exactly what user requested?
- [ ] Is there a rollback plan?

**If ANY answer is unclear → REQUIRE CONFIRMATION**

---

*Last updated: 2026-04-25*  
*Applies to: All Claude agent operations*
