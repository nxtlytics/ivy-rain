# How to use this script

**NOTE:** If you do not need to docker to authenticate against a registry do not enter anything for  `config/infrastructure/docker_config`

## 1. From your local machine

```shell
$ pbcopby < consul-kv-tool.py || cat consul-kv-tool.py | xclip
$ ssh <machine where ivy is already working>
```

## 2. ssh to an instance where ivy is already running

```shell
$ vim consul-kv-tool.py # paste the content of the script
$ python3 -m venv venv || python3 -m virtualenv venv
$ source ./venv/bin/activate
$ pip install requests
$ CONSUL_HTTP_TOKEN='<VALID TOKEN>' python consul-kv-tool.py export --path "proxy/" > proxy_kv.json
$ CONSUL_HTTP_TOKEN='<VALID TOKEN>' python consul-kv-tool.py export --path "config/" > config_kv.json
$ CONSUL_HTTP_TOKEN='<VALID TOKEN>' python consul-kv-tool.py export --path "service/" > service_kv.json
$ exit
```

## 3. From your local machine again

```shell
$ scp <machine where ivy is already working>:/path/to/json/exports/*.json .
$ vim -O *.json # edit files and remove sysenv specific stuff
$ scp ./*.json <machine where ivy is not fully setup yet>:/path/to/json/imports/
```

## 4. From an instance where ivy is not fully setup yet

```shell
$ vim consul-kv-tool.py # paste the content of the script
$ python3 -m venv venv || python3 -m virtualenv venv
$ source ./venv/bin/activate
$ pip install requests
$ for i in $(ls *.json); do CONSUL_HTTP_TOKEN='<VALID TOKEN>' python consul-kv-tool.py import -f "./${i}"; done
$ exit
```

## 5. Clean up (Local Machine, Instance where ivy was setup and Instance where ivy was not fully setup)

```shell
$ rm consul-kv-tool.py *.json venv/ # venv on ivy instances not locally
```
