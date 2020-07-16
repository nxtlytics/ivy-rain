# Working with PostgreSQL

## How to create a DB server

```
DB_<db name>_PASS=<MASTER PASSWORD> AWS_PROFILE=<PROFILE NAME> ./rain.py <short sysenv> apply --template RDS
```

Example:

```
DB_app_PASS=somepassword AWS_PROFILE=gov-tools ./rain.py govtools apply --template RDS
```

## Create database, users and, optionally, enable postgis

Requirements:

- [psql](https://www.postgresql.org/) `brew install postgresql`
- [1Password cli](https://1password.com/downloads/command-line/) `brew cask install 1password-cli`
- [jq](https://stedolan.github.io/jq/) `brew install jq`
- Write permission to `Temporary` vault in 1Password

```
$ ./create-db-and-basic-users.sh -h
Usage:
create-db-and-basic-users.sh -u <REQUIRED: administrator username> -p <REQUIRED: administrator password>
                             -h <REQUIRED: psql hostname, port is optional (host[:port])>
                             -d <REQUIRED: New database name> -e <Optional, enable postgis>

Creates postgresql database, admin, read, app users
Note: this uses 1Password's op command line.
      Please run: `eval $(op signin <1Password account name>)`
```


## Related documents

- [AWS PostGIS Instructions](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Appendix.PostgreSQL.CommonDBATasks.html#Appendix.PostgreSQL.CommonDBATasks.PostGIS)
