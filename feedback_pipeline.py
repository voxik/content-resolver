#!/usr/bin/python3

import argparse, yaml, tempfile, os, subprocess, json, jinja2, datetime, copy, re, dnf, pprint, urllib.request, sys
import concurrent.futures
import rpm_showme as showme
from functools import lru_cache
import eln_repo_split as reposplit


# Features of this new release
# - multiarch from the ground up!
# - more resilient
# - better internal data structure
# - user-defined views


###############################################################################
### Help ######################################################################
###############################################################################


# Configs:
#   TYPE:           KEY:          ID:
# - repo            repos         repo_id
# - env_conf        envs          env_id
# - workload_conf   workloads     workload_id
# - label           labels        label_id
# - conf_view       views         view_id
#
# Data:
#   TYPE:         KEY:                 ID:
# - pkg           pkgs/repo_id/arch    NEVR
# - env           envs                 env_id:repo_id:arch_id
# - workload      workloads            workload_id:env_id:repo_id:arch_id
# - view          views                view_id:repo_id:arch_id
#
#
#



###############################################################################
### Some initial stuff ########################################################
###############################################################################

# Error in global settings for Feedback Pipeline
# Settings to be implemented, now hardcoded below
class SettingsError(Exception):
    pass

# Error in user-provided configs
class ConfigError(Exception):
    pass

# Error in downloading repodata
class RepoDownloadError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr)

def err_log(msg):
    print("ERROR LOG:  {}".format(msg), file=sys.stderr)

class SetEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
        return json.JSONEncoder.default(self, obj)

def dump_data(path, data):
    with open(path, 'w') as file:
        json.dump(data, file, cls=SetEncoder)


def load_data(path):
    with open(path, 'r') as file:
        data = json.load(file)
    return data

def size(num, suffix='B'):
    for unit in ['','k','M','G']:
        if abs(num) < 1024.0:
            return "%3.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f %s%s" % (num, 'T', suffix)

def pkg_id_to_name(pkg_id):
    pkg_name = pkg_id.rsplit("-",2)[0]
    return pkg_name

def pkg_placeholder_name_to_id(placeholder_name):
    placeholder_id = "{name}-000-placeholder.placeholder".format(name=placeholder_name)
    return placeholder_id

def datetime_now_string():
    return datetime.datetime.now().strftime("%m/%d/%Y, %H:%M:%S")

def load_settings():
    settings = {}

    parser = argparse.ArgumentParser()
    parser.add_argument("configs", help="Directory with YAML configuration files. Only files ending with '.yaml' are accepted.")
    parser.add_argument("output", help="Directory to contain the output.")
    parser.add_argument("--use-cache", dest="use_cache", action='store_true', help="Use local data instead of pulling Content Resolver. Saves a lot of time! Needs a 'cache_data.json' file at the same location as the script is at.")
    parser.add_argument("--dnf-cache-dir", dest="dnf_cache_dir_override", help="Override the dnf cache_dir.")
    args = parser.parse_args()

    settings["configs"] = args.configs
    settings["output"] = args.output
    settings["use_cache"] = args.use_cache
    settings["dnf_cache_dir_override"] = args.dnf_cache_dir_override

    settings["allowed_arches"] = ["armv7hl","aarch64","ppc64le","s390x","x86_64"]

    settings["repos"] = {
        "appstream": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "baseos": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "crb": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "addon-ha": ["aarch64", "ppc64le", "s390x", "x86_64"],
        "addon-nfv": ["x86_64"],
        "addon-rt": ["x86_64"],
        "addon-rs": ["ppc64le", "s390x", "x86_64"],
        "addon-sap": ["ppc64le", "s390x", "x86_64"],
        "addon-saphana": ["ppc64le", "x86_64"]
    }

    settings["addons"] = ["addon-ha", "addon-nfv", "addon-rt", "addon-rs", "addon-sap", "addon-saphana"]

    return settings




###############################################################################
### Loading user-provided configs #############################################
###############################################################################

# Configs:
#   TYPE:         KEY:          ID:
# - repo          repos         repo_id
# - env           envs          env_id
# - workload      workloads     workload_id
# - label         labels        label_id
# - view          views         view_id

def _load_config_repo(document_id, document, settings):
    raise NotImplementedError("Repo v1 is not supported. Please migrate to repo v2.")

def _load_config_repo_v2(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Where does this repository come from?
        # Right now, only Fedora repositories are supported,
        # defined by their releasever.
        config["source"] = {}
        config["source"]["repos"] = {}
        if "repos" not in config["source"]:
            raise KeyError

        # Only Fedora repos supported at this time.
        # Fedora release.
        config["source"]["releasever"] = str(document["data"]["source"]["releasever"])

        # List of architectures
        config["source"]["architectures"] = []
        for arch_raw in document["data"]["source"]["architectures"]:
            arch = str(arch_raw)
            if arch not in settings["allowed_arches"]:
                err_log("Warning: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch))
                continue
            config["source"]["architectures"].append(str(arch))
    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=document_id))
    

    for id, repo_data in document["data"]["source"]["repos"].items():
        name = repo_data.get("name", id)
        priority = repo_data.get("priority", 100)
        limit_arches = repo_data.get("limit_arches", None)

        config["source"]["repos"][id] = {}
        config["source"]["repos"][id]["id"] = id
        config["source"]["repos"][id]["name"] = name
        try:
            config["source"]["repos"][id]["baseurl"] = repo_data["baseurl"]
        except KeyError:
            raise ConfigError("Error: {file} is invalid. Repo {id} doesn't list baseurl".format(
                file=yml_file,
                id=id))
        config["source"]["repos"][id]["priority"] = priority
        config["source"]["repos"][id]["limit_arches"] = limit_arches

    # Step 2: Optional fields

    config["source"]["composeinfo"] = document["data"]["source"].get("composeinfo", None)

    return config


def _load_config_env(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Different instances of the environment, one per repository.
        config["repositories"] = []
        for repo in document["data"]["repositories"]:
            config["repositories"].append(str(repo))
        
        # Packages defining this environment.
        # This list includes packages for all
        # architectures — that's the one to use by default.
        config["packages"] = []
        for pkg in document["data"]["packages"]:
            config["packages"].append(str(pkg))
        
        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=document_id))

    # Step 2: Optional fields

    # Architecture-specific packages.
    config["arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["arch_packages"][arch] = []
    if "arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Warning: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["arch_packages"][arch].append(pkg)
    
    # Extra installation options.
    # The following are now supported:
    # - "include-docs" - include documentation packages
    # - "include-weak-deps" - automatically pull in "recommends" weak dependencies
    config["options"] = []
    if "options" in document["data"]:
        if "include-docs" in document["data"]["options"]:
            config["options"].append("include-docs")
        if "include-weak-deps" in document["data"]["options"]:
            config["options"].append("include-weak-deps")

    return config


def _load_config_workload(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])
        
        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=document_id))

    # Step 2: Optional fields

    # Packages defining this workload.
    # This list includes packages for all
    # architectures — that's the one to use by default.
    config["packages"] = []
    # This workaround allows for "packages" to be left empty in the config
    try:
        for pkg in document["data"]["packages"]:
            config["packages"].append(str(pkg))
    except (TypeError, KeyError):
        err_log("Warning: {file} has an empty 'packages' field defined which is invalid. Moving on...".format(
            file=document_id
        ))

    # Architecture-specific packages.
    config["arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["arch_packages"][arch] = []
    if "arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            # This workaround allows for "arch_packages/ARCH" to be left empty in the config
            try:
                for pkg_raw in pkgs:
                    pkg = str(pkg_raw)
                    config["arch_packages"][arch].append(pkg)
            except TypeError:
                err_log("Warning: {file} has an empty 'arch_packages/{arch}' field defined which is invalid. Moving on...".format(
                    file=document_id,
                    arch=arch
                ))
    
    # Extra installation options.
    # The following are now supported:
    # - "include-docs" - include documentation packages
    # - "include-weak-deps" - automatically pull in "recommends" weak dependencies
    config["options"] = []
    if "options" in document["data"]:
        if "include-docs" in document["data"]["options"]:
            config["options"].append("include-docs")
        if "include-weak-deps" in document["data"]["options"]:
            config["options"].append("include-weak-deps")
    
    # Disable module streams.
    config["modules_disable"] = []
    if "modules_disable" in document["data"]:
        for module in document["data"]["modules_disable"]:
            config["modules_disable"].append(module)
    
    # Enable module streams.
    config["modules_enable"] = []
    if "modules_enable" in document["data"]:
        for module in document["data"]["modules_enable"]:
            config["modules_enable"].append(module)
    
    # Comps groups
    config["groups"] = []
    if "groups" in document["data"]:
        for module in document["data"]["groups"]:
            config["groups"].append(module)

    # Package placeholders
    # Add packages to the workload that don't exist (yet) in the repositories.
    config["package_placeholders"] = {}
    if "package_placeholders" in document["data"]:
        for pkg_name, pkg_data in document["data"]["package_placeholders"].items():
            pkg_description = pkg_data.get("description", "Description not provided.")
            pkg_requires = pkg_data.get("requires", [])
            pkg_buildrequires = pkg_data.get("buildrequires", [])
            limit_arches = pkg_data.get("limit_arches", None)
            srpm = pkg_data.get("srpm", "")

            config["package_placeholders"][pkg_name] = {}
            config["package_placeholders"][pkg_name]["name"] = pkg_name
            config["package_placeholders"][pkg_name]["description"] = pkg_description
            config["package_placeholders"][pkg_name]["requires"] = pkg_requires
            config["package_placeholders"][pkg_name]["buildrequires"] = pkg_buildrequires
            config["package_placeholders"][pkg_name]["limit_arches"] = limit_arches
            config["package_placeholders"][pkg_name]["srpm"] = srpm

    return config


def _load_config_label(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields
    # none here

    return config


def _load_config_compose_view(document_id, document, settings):
    config = {}
    config["id"] = document_id
    config["type"] = "compose"

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))

        # Choose one repository that gets used as a source.
        config["repository"] = str(document["data"]["repository"])

    except KeyError:
        raise ConfigError("Error: {document_id}.yml is invalid.".format(document_id=document_id))

    # Step 2: Optional fields
    
    # Limit this view only to the following architectures
    config["architectures"] = []
    if "architectures" in document["data"]:
        for repo in document["data"]["architectures"]:
            config["architectures"].append(str(repo))
    
    # Packages to be flagged as unwanted
    config["unwanted_packages"] = []
    if "unwanted_packages" in document["data"]:
        for pkg in document["data"]["unwanted_packages"]:
            config["unwanted_packages"].append(str(pkg))

    # Packages to be flagged as unwanted  on specific architectures
    config["unwanted_arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["unwanted_arch_packages"][arch] = []
    if "unwanted_arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["unwanted_arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["unwanted_arch_packages"][arch].append(pkg)
    
    # SRPMs (components) to be flagged as unwanted
    config["unwanted_source_packages"] = []
    if "unwanted_source_packages" in document["data"]:
        for pkg in document["data"]["unwanted_source_packages"]:
            config["unwanted_source_packages"].append(str(pkg))

    return config


def _load_config_unwanted(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Name is an identifier for humans
        config["name"] = str(document["data"]["name"])

        # A short description, perhaps hinting the purpose
        config["description"] = str(document["data"]["description"])

        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # Labels connect things together.
        # Workloads get installed in environments with the same label.
        # They also get included in views with the same label.
        config["labels"] = []
        for repo in document["data"]["labels"]:
            config["labels"].append(str(repo))
    
    except KeyError:
        raise ConfigError("Error: {document_id}.yml is invalid.".format(document_id=document_id))
    
    # Step 2: Optional fields

    # Packages to be flagged as unwanted
    config["unwanted_packages"] = []
    if "unwanted_packages" in document["data"]:
        for pkg in document["data"]["unwanted_packages"]:
            config["unwanted_packages"].append(str(pkg))

    # Packages to be flagged as unwanted  on specific architectures
    config["unwanted_arch_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["unwanted_arch_packages"][arch] = []
    if "unwanted_arch_packages" in document["data"]:
        for arch, pkgs in document["data"]["unwanted_arch_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["unwanted_arch_packages"][arch].append(pkg)
    
    # SRPMs (components) to be flagged as unwanted
    config["unwanted_source_packages"] = []
    if "unwanted_source_packages" in document["data"]:
        for pkg in document["data"]["unwanted_source_packages"]:
            config["unwanted_source_packages"].append(str(pkg))

    # SRPMs (components) to be flagged as unwanted on specific architectures
    config["unwanted_arch_source_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["unwanted_arch_source_packages"][arch] = []
    if "unwanted_arch_source_packages" in document["data"]:
        for arch, pkgs in document["data"]["unwanted_arch_source_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            for pkg_raw in pkgs:
                pkg = str(pkg_raw)
                config["unwanted_arch_source_packages"][arch].append(pkg)
    return config


def _load_config_buildroot(document_id, document, settings):
    config = {}
    config["id"] = document_id

    # Step 1: Mandatory fields
    try:
        # Who maintains it? This is just a freeform string
        # for humans to read. In Fedora, a FAS nick is recommended.
        config["maintainer"] = str(document["data"]["maintainer"])

        # What view is this for
        config["view_id"] = str(document["data"]["view_id"])

    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))

    # Step 2: Optional fields
    config["base_buildroot"] = {}
    for arch in settings["allowed_arches"]:
        config["base_buildroot"][arch] = []
    if "base_buildroot" in document["data"]:
        for arch, pkgs in document["data"]["base_buildroot"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            if pkgs:
                for pkg_raw in pkgs:
                    pkg = str(pkg_raw)
                    config["base_buildroot"][arch].append(pkg)

    config["source_packages"] = {}
    for arch in settings["allowed_arches"]:
        config["source_packages"][arch] = {}
    if "source_packages" in document["data"]:
        for arch, srpms_dict in document["data"]["source_packages"].items():
            if arch not in settings["allowed_arches"]:
                err_log("Error: {file}.yaml lists an invalid architecture: {arch}. Ignoring.".format(
                    file=document_id,
                    arch=arch
                ))
                continue
            if not srpms_dict:
                continue
            for srpm_name, srpm_data in srpms_dict.items():
                requires = []
                if "requires" in srpm_data:
                    try:
                        for pkg_raw in srpm_data["requires"]:
                            requires.append(str(pkg_raw))
                    except TypeError:
                        err_log("Warning: {file} has an empty 'requires' field defined which is invalid. Moving on...".format(
                            file=document_id
                        ))
                        continue
                
                config["source_packages"][arch][str(srpm_name)] = {}
                config["source_packages"][arch][str(srpm_name)]["requires"] = requires

    return config


def _load_json_data_buildroot_pkg_relations(document_id, document, settings):
    config = {}
    config["id"] = document_id

    try:
        # View ID
        config["view_id"] = document["data"]["view_id"]

        # Arch
        arch = document["data"]["arch"]
        if arch not in settings["allowed_arches"]:
            raise ConfigError("Error: {file}.json lists an invalid architecture: {arch}. Ignoring this file.".format(
                file=document_id,
                arch=arch
            ))
        config["arch"] = arch

        #pkg_relations
        config["pkg_relations"] = document["data"]["pkgs"]
        
    except KeyError:
        raise ConfigError("Error: {file} is invalid.".format(file=yml_file))
    
    return config


def get_configs(settings):
    log("")
    log("###############################################################################")
    log("### Loading user-provided configs #############################################")
    log("###############################################################################")
    log("")

    directory = settings["configs"]

    if "allowed_arches" not in settings:
        err_log("System error: allowed_arches not configured")
        raise SettingsError
    
    if not settings["allowed_arches"]:
        err_log("System error: no allowed_arches not configured")
        raise SettingsError

    configs = {}

    configs["repos"] = {}
    configs["envs"] = {}
    configs["workloads"] = {}
    configs["labels"] = {}
    configs["views"] = {}
    configs["unwanteds"] = {}
    configs["buildroots"] = {}
    configs["buildroot_pkg_relations"] = {}

    # Step 1: Load all configs
    log("Loading config files...")
    for yml_file in os.listdir(directory):
        # Only accept yaml files
        if not yml_file.endswith(".yaml"):
            continue
        
        document_id = yml_file.split(".yaml")[0]

        try:
            with open(os.path.join(directory, yml_file), "r") as file:
                # Safely load the config
                try:
                    document = yaml.safe_load(file)
                except yaml.YAMLError as err:
                    raise ConfigError("Error loading a config '{filename}': {err}".format(
                                filename=yml_file,
                                err=err))
                
                # Only accept yaml files stating their purpose!
                if not ("document" in document and "version" in document):
                    raise ConfigError("Error: {file} is invalid.".format(file=yml_file))


                # === Case: Repository config ===
                if document["document"] == "feedback-pipeline-repository":
                    if document["version"] == 1:
                        configs["repos"][document_id] = _load_config_repo(document_id, document, settings)
                    
                    elif document["version"] == 2:
                        configs["repos"][document_id] = _load_config_repo_v2(document_id, document, settings)

                # === Case: Environment config ===
                if document["document"] == "feedback-pipeline-environment":
                    configs["envs"][document_id] = _load_config_env(document_id, document, settings)

                # === Case: Workload config ===
                if document["document"] == "feedback-pipeline-workload":
                    configs["workloads"][document_id] = _load_config_workload(document_id, document, settings)
                
                # === Case: Label config ===
                if document["document"] == "feedback-pipeline-label":
                    configs["labels"][document_id] = _load_config_label(document_id, document, settings)

                # === Case: View config ===
                if document["document"] == "feedback-pipeline-compose-view":
                    configs["views"][document_id] = _load_config_compose_view(document_id, document, settings)

                # === Case: Unwanted config ===
                if document["document"] == "feedback-pipeline-unwanted":
                    configs["unwanteds"][document_id] = _load_config_unwanted(document_id, document, settings)

                # === Case: Buildroot config ===
                if document["document"] == "feedback-pipeline-buildroot":
                    configs["buildroots"][document_id] = _load_config_buildroot(document_id, document, settings)

        except ConfigError as err:
            err_log("Config load error: {err}. Ignoring.".format(err=err))
            continue
    
    # Step 1.5: Load all external data sources
    log("Loading external data files...")
    for json_file in os.listdir(directory):
        # Only accept yaml files
        if not json_file.endswith(".json"):
            continue
        
        document_id = json_file.split(".json")[0]

        try:
            try:
                json_data = load_data(os.path.join(directory, json_file))
            except:
                raise ConfigError("Error loading a JSON data file '{filename}': {err}".format(
                                filename=json_file,
                                err=err))
            
            # Only accept json files stating their purpose!
            if not ("document_type" in json_data and "version" in json_data):
                raise ConfigError("Error: {file} is invalid.".format(file=json_file))


            # === Case: Buildroot pkg relations data ===
            if json_data["document_type"] == "buildroot-binary-relations":
                configs["buildroot_pkg_relations"][document_id] = _load_json_data_buildroot_pkg_relations(document_id, json_data, settings)


        except ConfigError as err:
            err_log("JSON data load error: {err}. Ignoring.".format(err=err))
            continue

        


    
    log("  Done!")
    log("")

    # Step 2: cross check configs for references and other validation
    log("  Validating configs...")
    # FIXME: Do this, please!
    log("  Warning: This is not implemented, yet!")
    log("           But there would be a traceback somewhere during runtime ")
    log("           if an error exists, so wrong outputs won't happen.")

    log("  Done!")
    log("")
    
    log("Done!  Loaded:")
    log("  - {} repositories".format(len(configs["repos"])))
    log("  - {} environments".format(len(configs["envs"])))
    log("  - {} workloads".format(len(configs["workloads"])))
    log("  - {} labels".format(len(configs["labels"])))
    log("  - {} views".format(len(configs["views"])))
    log("  - {} exclusion lists".format(len(configs["unwanteds"])))
    log("  - {} buildroots".format(len(configs["buildroots"])))
    log("")
    log("And the following data JSONs:")
    log("  - {} buildroot pkg relations".format(len(configs["buildroot_pkg_relations"])))
    log("")
    


    return configs



###############################################################################
### Analyzing stuff! ##########################################################
###############################################################################

# Configs:
#   TYPE:           KEY:          ID:
# - repo            repos         repo_id
# - env_conf        envs          env_id
# - workload_conf   workloads     workload_id
# - label           labels        label_id
# - conf_view       views         view_id
#
# Data:
#   TYPE:         KEY:                 ID:
# - pkg           pkgs/repo_id/arch    NEVR
# - env           envs                 env_id:repo_id:arch_id
# - workload      workloads            workload_id:env_id:repo_id:arch_id
# - view          views                view_id:repo_id:arch_id
#
# tmp contents:
# - dnf_cachedir-{repo}-{arch}
# - dnf_generic_installroot-{repo}-{arch}
# - dnf_env_installroot-{env_conf}-{repo}-{arch}
#
#

global_dnf_repo_cache = {}
def _load_repo_cached(base, repo, arch):
    repo_id = repo["id"]

    exists = True
    
    if repo_id not in global_dnf_repo_cache:
        exists = False
        global_dnf_repo_cache[repo_id] = {}

    elif arch not in global_dnf_repo_cache[repo_id]:
        exists = False
    
    if exists:
        log("  Loading repos from cache...")

        for repo in global_dnf_repo_cache[repo_id][arch]:
            base.repos.add(repo)

    else:
        log("  Loading repos using DNF...")

        for repo_name, repo_data in repo["source"]["repos"].items():
            if repo_data["limit_arches"]:
                if arch not in repo_data["limit_arches"]:
                    log("  Skipping {} on {}".format(repo_name, arch))
                    continue
            log("  Including {}".format(repo_name))

            additional_repo = dnf.repo.Repo(
                name=repo_name,
                parent_conf=base.conf
            )
            additional_repo.baseurl = repo_data["baseurl"]
            additional_repo.priority = repo_data["priority"]
            base.repos.add(additional_repo)

        # Additional repository (if configured)
        #if repo["source"]["additional_repository"]:
        #    additional_repo = dnf.repo.Repo(name="additional-repository",parent_conf=base.conf)
        #    additional_repo.baseurl = [repo["source"]["additional_repository"]]
        #    additional_repo.priority = 1
        #    base.repos.add(additional_repo)

        # All other system repos
        #base.read_all_repos()

        global_dnf_repo_cache[repo_id][arch] = []
        for repo in base.repos.iter_enabled():
            global_dnf_repo_cache[repo_id][arch].append(repo)
    
    


def _analyze_pkgs(tmp_dnf_cachedir, tmp_installroots, repo, arch):
    log("Analyzing pkgs for {repo_name} ({repo_id}) {arch}".format(
            repo_name=repo["name"],
            repo_id=repo["id"],
            arch=arch
        ))
    
    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp_dnf_cachedir, cachedir_name)

        # Generic installroot
        root_name = "dnf_generic_installroot-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp_installroots, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Releasever
        base.conf.substitutions['releasever'] = repo["source"]["releasever"]

        for repo_name, repo_data in repo["source"]["repos"].items():
            if repo_data["limit_arches"]:
                if arch not in repo_data["limit_arches"]:
                    log("  Skipping {} on {}".format(repo_name, arch))
                    continue
            log("  Including {}".format(repo_name))

            additional_repo = dnf.repo.Repo(
                name=repo_name,
                parent_conf=base.conf
            )
            additional_repo.baseurl = repo_data["baseurl"]
            additional_repo.priority = repo_data["priority"]
            base.repos.add(additional_repo)

        # Additional repository (if configured)
        #if repo["source"]["additional_repository"]:
        #    additional_repo = dnf.repo.Repo(name="additional-repository",parent_conf=base.conf)
        #    additional_repo.baseurl = [repo["source"]["additional_repository"]]
        #    additional_repo.priority = 1
        #    base.repos.add(additional_repo)

        # Load repos
        log("  Loading repos...")
        #base.read_all_repos()


        # At this stage, I need to get all packages from the repo listed.
        # That also includes modular packages. Modular packages in non-enabled
        # streams would be normally hidden. So I mark all the available repos as
        # hotfix repos to make all packages visible, including non-enabled streams.
        for repo in base.repos.all():
            repo.module_hotfixes = True

        # This sometimes fails, so let's try at least N times
        # before totally giving up!
        MAX_TRIES = 10
        attempts = 0
        success = False
        while attempts < MAX_TRIES:
            try:
                base.fill_sack(load_system_repo=False)
                success = True
                break
            except dnf.exceptions.RepoError as err:
                attempts +=1
                log("  Failed to download repodata. Trying again!")
        if not success:
            err = "Failed to download repodata while analyzing repo '{repo_name} ({repo_id}) {arch}".format(
            repo_name=repo["name"],
            repo_id=repo["id"],
            arch=arch
            )
            err_log(err)
            raise RepoDownloadError(err)

        # DNF query
        query = base.sack.query

        # Get all packages
        all_pkgs_set = set(query())
        pkgs = {}
        for pkg_object in all_pkgs_set:
            pkg_nevra = "{name}-{evr}.{arch}".format(
                name=pkg_object.name,
                evr=pkg_object.evr,
                arch=pkg_object.arch)
            pkg = {}
            pkg["id"] = pkg_nevra
            pkg["name"] = pkg_object.name
            pkg["evr"] = pkg_object.evr
            pkg["arch"] = pkg_object.arch
            pkg["installsize"] = pkg_object.installsize
            pkg["description"] = pkg_object.description
            #pkg["provides"] = pkg_object.provides
            #pkg["requires"] = pkg_object.requires
            #pkg["recommends"] = pkg_object.recommends
            #pkg["suggests"] = pkg_object.suggests
            pkg["summary"] = pkg_object.summary
            pkg["source_name"] = pkg_object.source_name
            pkg["sourcerpm"] = pkg_object.sourcerpm
            pkgs[pkg_nevra] = pkg
        
        log("  Done!  ({pkg_count} packages in total)".format(
            pkg_count=len(pkgs)
        ))
        log("")

    return pkgs

def _analyze_package_relations(dnf_query, package_placeholders = None):
    relations = {}

    for pkg in dnf_query:
        pkg_id = "{name}-{evr}.{arch}".format(
            name=pkg.name,
            evr=pkg.evr,
            arch=pkg.arch
        )
        
        required_by = set()
        recommended_by = set()
        suggested_by = set()

        for dep_pkg in dnf_query.filter(requires=pkg.provides):
            dep_pkg_id = "{name}-{evr}.{arch}".format(
                name=dep_pkg.name,
                evr=dep_pkg.evr,
                arch=dep_pkg.arch
            )
            required_by.add(dep_pkg_id)

        for dep_pkg in dnf_query.filter(recommends=pkg.provides):
            dep_pkg_id = "{name}-{evr}.{arch}".format(
                name=dep_pkg.name,
                evr=dep_pkg.evr,
                arch=dep_pkg.arch
            )
            recommended_by.add(dep_pkg_id)
        
        for dep_pkg in dnf_query.filter(suggests=pkg.provides):
            dep_pkg_id = "{name}-{evr}.{arch}".format(
                name=dep_pkg.name,
                evr=dep_pkg.evr,
                arch=dep_pkg.arch
            )
            suggested_by.add(dep_pkg_id)
        
        relations[pkg_id] = {}
        relations[pkg_id]["required_by"] = sorted(list(required_by))
        relations[pkg_id]["recommended_by"] = sorted(list(recommended_by))
        relations[pkg_id]["suggested_by"] = sorted(list(suggested_by))
        relations[pkg_id]["source_name"] = pkg.source_name
        relations[pkg_id]["reponame"] = pkg.reponame
    
    if package_placeholders:
        for placeholder_name,placeholder_data in package_placeholders.items():
            placeholder_id = pkg_placeholder_name_to_id(placeholder_name)

            relations[placeholder_id] = {}
            relations[placeholder_id]["required_by"] = []
            relations[placeholder_id]["recommended_by"] = []
            relations[placeholder_id]["suggested_by"] = []
            relations[placeholder_id]["reponame"] = None
        
        for placeholder_name,placeholder_data in package_placeholders.items():
            placeholder_id = pkg_placeholder_name_to_id(placeholder_name)
            for placeholder_dependency_name in placeholder_data["requires"]:
                for pkg_id in relations:
                    pkg_name = pkg_id_to_name(pkg_id)
                    if pkg_name == placeholder_dependency_name:
                        relations[pkg_id]["required_by"].append(placeholder_id)
    
    return relations

def _analyze_env(tmp_dnf_cachedir, tmp_installroots, env_conf, repo, arch):
    env = {}
    
    env["env_conf_id"] = env_conf["id"]
    env["pkg_ids"] = []
    env["repo_id"] = repo["id"]
    env["arch"] = arch

    env["pkg_relations"] = []

    env["errors"] = {}
    env["errors"]["non_existing_pkgs"] = []

    env["succeeded"] = True

    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp_dnf_cachedir, cachedir_name)

        # Environment installroot
        root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
            env_conf=env_conf["id"],
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp_installroots, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Releasever
        base.conf.substitutions['releasever'] = repo["source"]["releasever"]

        # Additional DNF Settings
        base.conf.tsflags.append('justdb')
        base.conf.tsflags.append('noscripts')

        # Environment config
        if "include-weak-deps" not in env_conf["options"]:
            base.conf.install_weak_deps = False
        if "include-docs" not in env_conf["options"]:
            base.conf.tsflags.append('nodocs')

        # Load repos
        #log("  Loading repos...")
        #base.read_all_repos()
        _load_repo_cached(base, repo, arch)

        # This sometimes fails, so let's try at least N times
        # before totally giving up!
        MAX_TRIES = 10
        attempts = 0
        success = False
        while attempts < MAX_TRIES:
            try:
                base.fill_sack(load_system_repo=False)
                success = True
                break
            except dnf.exceptions.RepoError as err:
                attempts +=1
                log("  Failed to download repodata. Trying again!")
        if not success:
            err = "Failed to download repodata while analyzing environment '{env_conf}' from '{repo}' {arch}:".format(
                env_conf=env_conf["id"],
                repo=repo["id"],
                arch=arch
            )
            err_log(err)
            raise RepoDownloadError(err)


        # Packages
        log("  Adding packages...")
        for pkg in env_conf["packages"]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                env["errors"]["non_existing_pkgs"].append(pkg)
                continue

        # Architecture-specific packages
        for pkg in env_conf["arch_packages"][arch]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                env["errors"]["non_existing_pkgs"].append(pkg)
                continue
        
        # Resolve dependencies
        log("  Resolving dependencies...")
        try:
            base.resolve()
        except dnf.exceptions.DepsolveError as err:
            err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                    env_conf=env_conf["id"],
                    repo=repo["id"],
                    arch=arch
                ))
            err_log("  - {err}".format(err=err))
            env["succeeded"] = False
            env["errors"]["message"] = str(err)
            return env

        # Write the result into RPMDB.
        # The transaction needs us to download all the packages. :(
        # So let's do that to make it happy.
        log("  Downloading packages...")
        base.download_packages(base.transaction.install_set)
        log("  Running DNF transaction, writing RPMDB...")
        try:
            base.do_transaction()
        except (dnf.exceptions.TransactionCheckError, dnf.exceptions.Error) as err:
            err_log("Failed to analyze environment '{env_conf}' from '{repo}' {arch}:".format(
                    env_conf=env_conf["id"],
                    repo=repo["id"],
                    arch=arch
                ))
            err_log("  - {err}".format(err=err))
            env["succeeded"] = False
            env["errors"]["message"] = str(err)
            return env

        # DNF Query
        log("  Creating a DNF Query object...")
        query = base.sack.query().filterm(pkg=base.transaction.install_set)

        for pkg in query:
            pkg_id = "{name}-{evr}.{arch}".format(
                name=pkg.name,
                evr=pkg.evr,
                arch=pkg.arch
            )
            env["pkg_ids"].append(pkg_id)
        
        env["pkg_relations"] = _analyze_package_relations(query)

        log("  Done!  ({pkg_count} packages in total)".format(
            pkg_count=len(env["pkg_ids"])
        ))
        log("")
    
    return env


def _analyze_envs(tmp_dnf_cachedir, tmp_installroots, configs):
    envs = {}

    # Look at all env configs...
    for env_conf_id, env_conf in configs["envs"].items():
        # For each of those, look at all repos it lists...
        for repo_id in env_conf["repositories"]:
            # And for each of the repo, look at all arches it supports.
            repo = configs["repos"][repo_id]
            for arch in repo["source"]["architectures"]:
                # Now it has
                #    all env confs *
                #    repos each config lists *
                #    archeas each repo supports
                # Analyze all of that!
                log("Analyzing {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                    env_name=env_conf["name"],
                    env_id=env_conf_id,
                    repo_name=repo["name"],
                    repo=repo_id,
                    arch=arch
                ))

                env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                    env_conf_id=env_conf_id,
                    repo_id=repo_id,
                    arch=arch
                )
                envs[env_id] = _analyze_env(tmp_dnf_cachedir, tmp_installroots, env_conf, repo, arch)
                
    
    return envs


def _return_failed_workload_env_err(workload_conf, env_conf, repo, arch):
    workload = {}

    workload["workload_conf_id"] = workload_conf["id"]
    workload["env_conf_id"] = env_conf["id"]
    workload["repo_id"] = repo["id"]
    workload["arch"] = arch

    workload["pkg_env_ids"] = []
    workload["pkg_added_ids"] = []
    workload["pkg_placeholder_ids"] = []

    workload["pkg_relations"] = []

    workload["errors"] = {}
    workload["errors"]["non_existing_pkgs"] = []
    workload["succeeded"] = False
    workload["env_succeeded"] = False

    workload["errors"]["message"] = """
    Failed to analyze this workload because of an error while analyzing the environment.

    Please see the associated environment results for a detailed error message.
    """

    return workload


def _analyze_workload(tmp_dnf_cachedir, tmp_installroots, workload_conf, env_conf, repo, arch):
    workload = {}

    workload["workload_conf_id"] = workload_conf["id"]
    workload["env_conf_id"] = env_conf["id"]
    workload["repo_id"] = repo["id"]
    workload["arch"] = arch

    workload["pkg_env_ids"] = []
    workload["pkg_added_ids"] = []
    workload["pkg_placeholder_ids"] = []

    workload["enabled_modules"] = []

    workload["pkg_relations"] = []

    workload["errors"] = {}
    workload["errors"]["non_existing_pkgs"] = []
    workload["errors"]["non_existing_placeholder_deps"] = []

    workload["succeeded"] = True
    workload["env_succeeded"] = True

    with dnf.Base() as base:

        # Local DNF cache
        cachedir_name = "dnf_cachedir-{repo}-{arch}".format(
            repo=repo["id"],
            arch=arch
        )
        base.conf.cachedir = os.path.join(tmp_dnf_cachedir, cachedir_name)

        # Environment installroot
        # Since we're not writing anything into the installroot,
        # let's just use the base image's installroot!
        root_name = "dnf_env_installroot-{env_conf}-{repo}-{arch}".format(
            env_conf=env_conf["id"],
            repo=repo["id"],
            arch=arch
        )
        base.conf.installroot = os.path.join(tmp_installroots, root_name)

        # Architecture
        base.conf.arch = arch
        base.conf.ignorearch = True

        # Releasever
        base.conf.substitutions['releasever'] = repo["source"]["releasever"]

        # Environment config
        if "include-weak-deps" not in workload_conf["options"]:
            base.conf.install_weak_deps = False
        if "include-docs" not in workload_conf["options"]:
            base.conf.tsflags.append('nodocs')

        # Load repos
        #log("  Loading repos...")
        #base.read_all_repos()
        _load_repo_cached(base, repo, arch)

        # Now I need to load the local RPMDB.
        # However, if the environment is empty, it wasn't created, so I need to treat
        # it differently. So let's check!
        if len(env_conf["packages"]) or len(env_conf["arch_packages"][arch]):
            # It's not empty! Load local data.
            base.fill_sack(load_system_repo=True)
        else:
            # It's empty. Treat it like we're using an empty installroot.
            # This sometimes fails, so let's try at least N times
            # before totally giving up!
            MAX_TRIES = 10
            attempts = 0
            success = False
            while attempts < MAX_TRIES:
                try:
                    base.fill_sack(load_system_repo=False)
                    success = True
                    break
                except dnf.exceptions.RepoError as err:
                    attempts +=1
                    log("  Failed to download repodata. Trying again!")
            if not success:
                err = "Failed to download repodata while analyzing workload '{workload_id} on '{env_id}' from '{repo}' {arch}...".format(
                        workload_id=workload_conf_id,
                        env_id=env_conf_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch)
                err_log(err)
                raise RepoDownloadError(err)
        
        # Disabling modules
        if workload_conf["modules_disable"]:
            try:
                log("  Disabling modules...")
                module_base = dnf.module.module_base.ModuleBase(base)
                module_base.disable(workload_conf["modules_disable"])
            except dnf.exceptions.MarkingErrors as err:
                workload["succeeded"] = False
                workload["errors"]["message"] = str(err)
                log("  Failed!  (Error message will be on the workload results page.")
                log("")
                return workload


        # Enabling modules
        if workload_conf["modules_enable"]:
            try:
                log("  Enabling modules...")
                module_base = dnf.module.module_base.ModuleBase(base)
                module_base.enable(workload_conf["modules_enable"])
            except dnf.exceptions.MarkingErrors as err:
                workload["succeeded"] = False
                workload["errors"]["message"] = str(err)
                log("  Failed!  (Error message will be on the workload results page.")
                log("")
                return workload
        
        # Get a list of enabled modules
        # The official DNF API doesn't support it. I got this from the DNF folks
        # (thanks!) as a solution, but just keeping it in a generic try/except
        # as it's not an official API. 
        enabled_modules = set()
        try:
            all_modules = base._moduleContainer.getModulePackages()
            for module in all_modules:
                if base._moduleContainer.isEnabled(module):
                    module_name = module.getName()
                    module_stream = module.getStream()
                    module_nsv = "{module_name}:{module_stream}".format(
                        module_name=module_name,
                        module_stream=module_stream
                    )
                    enabled_modules.add(module_nsv)
        except:
            log("  Something went wrong with getting a list of enabled modules. (This uses non-API DNF calls. Skipping.)")
            enabled_modules = set()
        workload["enabled_modules"] = list(enabled_modules)


        # Packages
        log("  Adding packages...")
        for pkg in workload_conf["packages"]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                workload["errors"]["non_existing_pkgs"].append(pkg)
                continue
        
        # Groups
        log("  Adding groups...")
        if workload_conf["groups"]:
            base.read_comps(arch_filter=True)
        for grp_spec in workload_conf["groups"]:
            group = base.comps.group_by_pattern(grp_spec)
            if not group:
                workload["errors"]["non_existing_pkgs"].append(grp_spec)
                continue
            base.group_install(group.id, ['mandatory', 'default'])
        
        
            # TODO: Mark group packages as required... the following code doesn't work
            #for pkg in group.packages_iter():
            #    print(pkg.name)
            #    workload_conf["packages"].append(pkg.name)
               
                
        
        # Filter out the relevant package placeholders for this arch
        package_placeholders = {}
        for placeholder_name,placeholder_data in workload_conf["package_placeholders"].items():
            # If this placeholder is not limited to just a usbset of arches, add it
            if not placeholder_data["limit_arches"]:
                package_placeholders[placeholder_name] = placeholder_data
            # otherwise it is limited. In that case, only add it if the current arch is on its list
            elif arch in placeholder_data["limit_arches"]:
                package_placeholders[placeholder_name] = placeholder_data

        # Dependencies of package placeholders
        log("  Adding package placeholder dependencies...")
        for placeholder_name,placeholder_data in package_placeholders.items():
            for pkg in placeholder_data["requires"]:
                try:
                    base.install(pkg)
                except dnf.exceptions.MarkingError:
                    workload["errors"]["non_existing_placeholder_deps"].append(pkg)
                    continue

        # Architecture-specific packages
        for pkg in workload_conf["arch_packages"][arch]:
            try:
                base.install(pkg)
            except dnf.exceptions.MarkingError:
                workload["errors"]["non_existing_pkgs"].append(pkg)
                continue

        if workload["errors"]["non_existing_pkgs"] or workload["errors"]["non_existing_placeholder_deps"]:
            error_message_list = []
            if workload["errors"]["non_existing_pkgs"]:
                error_message_list.append("The following required packages are not available:")
                for pkg_name in workload["errors"]["non_existing_pkgs"]:
                    pkg_string = "  - {pkg_name}".format(
                        pkg_name=pkg_name
                    )
                    error_message_list.append(pkg_string)
            if workload["errors"]["non_existing_placeholder_deps"]:
                error_message_list.append("The following dependencies of package placeholders are not available:")
                for pkg_name in workload["errors"]["non_existing_placeholder_deps"]:
                    pkg_string = "  - {pkg_name}".format(
                        pkg_name=pkg_name
                    )
                    error_message_list.append(pkg_string)
            error_message = "\n".join(error_message_list)
            workload["succeeded"] = False
            workload["errors"]["message"] = str(error_message)
            log("  Failed!  (Error message will be on the workload results page.")
            log("")
            return workload

        # Resolve dependencies
        log("  Resolving dependencies...")
        try:
            base.resolve()
        except dnf.exceptions.DepsolveError as err:
            workload["succeeded"] = False
            workload["errors"]["message"] = str(err)
            log("  Failed!  (Error message will be on the workload results page.")
            log("")
            return workload

        # DNF Query
        log("  Creating a DNF Query object...")
        query_env = base.sack.query()
        query_added = base.sack.query().filterm(pkg=base.transaction.install_set)
        pkgs_env = set(query_env.installed())
        pkgs_added = set(base.transaction.install_set)
        pkgs_all = set.union(pkgs_env, pkgs_added)
        query_all = base.sack.query().filterm(pkg=pkgs_all)
        
        for pkg in pkgs_env:
            pkg_id = "{name}-{evr}.{arch}".format(
                name=pkg.name,
                evr=pkg.evr,
                arch=pkg.arch
            )
            workload["pkg_env_ids"].append(pkg_id)
        
        for pkg in pkgs_added:
            pkg_id = "{name}-{evr}.{arch}".format(
                name=pkg.name,
                evr=pkg.evr,
                arch=pkg.arch
            )
            workload["pkg_added_ids"].append(pkg_id)

        # No errors so far? That means the analysis has succeeded,
        # so placeholders can be added to the list as well.
        # (Failed workloads need to have empty results, that's why)
        for placeholder_name in package_placeholders:
            workload["pkg_placeholder_ids"].append(pkg_placeholder_name_to_id(placeholder_name))
        
        workload["pkg_relations"] = _analyze_package_relations(query_all, package_placeholders)
        
        pkg_env_count = len(workload["pkg_env_ids"])
        pkg_added_count = len(workload["pkg_added_ids"])
        log("  Done!  ({pkg_count} packages in total. That's {pkg_env_count} in the environment, and {pkg_added_count} added.)".format(
            pkg_count=str(pkg_env_count + pkg_added_count),
            pkg_env_count=pkg_env_count,
            pkg_added_count=pkg_added_count
        ))
        log("")

    return workload


def _analyze_workloads(tmp_dnf_cachedir, tmp_installroots, configs, data):
    workloads = {}

    # Here, I need to mix and match workloads & envs based on labels
    workload_env_map = {}
    # Look at all workload configs...
    for workload_conf_id, workload_conf in configs["workloads"].items():
        workload_env_map[workload_conf_id] = set()
        # ... and all of their labels.
        for label in workload_conf["labels"]:
            # And for each label, find all env configs...
            for env_conf_id, env_conf in configs["envs"].items():
                # ... that also have the label.
                if label in env_conf["labels"]:
                    # And save those.
                    workload_env_map[workload_conf_id].add(env_conf_id)
    
    # Get the total number of workloads
    number_of_workloads = 0
    # And now, look at all workload configs...
    for workload_conf_id, workload_conf in configs["workloads"].items():
        # ... and for each, look at all env configs it should be analyzed in.
        for env_conf_id in workload_env_map[workload_conf_id]:
            # Each of those envs can have multiple repos associated...
            env_conf = configs["envs"][env_conf_id]
            for repo_id in env_conf["repositories"]:
                # ... and each repo probably has multiple architecture.
                repo = configs["repos"][repo_id]
                arches = repo["source"]["architectures"]
                number_of_workloads += len(arches)

    # Analyze the workloads
    current_workload = 0
    # And now, look at all workload configs...
    for workload_conf_id, workload_conf in configs["workloads"].items():
        # ... and for each, look at all env configs it should be analyzed in.
        for env_conf_id in workload_env_map[workload_conf_id]:
            # Each of those envs can have multiple repos associated...
            env_conf = configs["envs"][env_conf_id]
            for repo_id in env_conf["repositories"]:
                # ... and each repo probably has multiple architecture.
                repo = configs["repos"][repo_id]
                for arch in repo["source"]["architectures"]:

                    current_workload += 1
                    log ("[ workload {current} of {total} ]".format(
                        current=current_workload,
                        total=number_of_workloads
                    ))

                    # And now it has:
                    #   all workload configs *
                    #   all envs that match those *
                    #   all repos of those envs *
                    #   all arches of those repos.
                    # That's a lot of stuff! Let's analyze all of that!
                    log("Analyzing {workload_name} ({workload_id}) on {env_name} ({env_id}) from {repo_name} ({repo}) {arch}...".format(
                        workload_name=workload_conf["name"],
                        workload_id=workload_conf_id,
                        env_name=env_conf["name"],
                        env_id=env_conf_id,
                        repo_name=repo["name"],
                        repo=repo_id,
                        arch=arch
                    ))

                    workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                        workload_conf_id=workload_conf_id,
                        env_conf_id=env_conf_id,
                        repo_id=repo_id,
                        arch=arch
                    )

                    # Before even started, look if the env succeeded. If not, there's
                    # no point in doing anything here.
                    env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                        env_conf_id=env_conf["id"],
                        repo_id=repo["id"],
                        arch=arch
                    )
                    env = data["envs"][env_id]
                    if env["succeeded"]:
                        # Let's do this! 

                        # DNF leaks memory and file descriptors :/
                        # 
                        # So, this workaround runs it in a subprocess that should have its resources
                        # freed when done!
                        with concurrent.futures.ProcessPoolExecutor(max_workers=1) as executor:
                            workloads[workload_id] = executor.submit(_analyze_workload, tmp_dnf_cachedir, tmp_installroots, workload_conf, env_conf, repo, arch).result()

                        #workloads[workload_id] = _analyze_workload(tmp_dnf_cachedir, tmp_installroots, workload_conf, env_conf, repo, arch)
                    
                    else:
                        workloads[workload_id] = _return_failed_workload_env_err(workload_conf, env_conf, repo, arch)



    return workloads


def analyze_things(configs, settings):
    log("")
    log("###############################################################################")
    log("### Analyzing stuff! ##########################################################")
    log("###############################################################################")
    log("")

    data = {}

    data["pkgs"] = {}
    data["envs"] = {}
    data["workloads"] = {}
    data["views"] = {}

    with tempfile.TemporaryDirectory() as tmp:

        if settings["dnf_cache_dir_override"]:
            tmp_dnf_cachedir = settings["dnf_cache_dir_override"]
        else:
            tmp_dnf_cachedir = os.path.join(tmp, "dnf_cachedir")
        tmp_installroots = os.path.join(tmp, "installroots")

        # List of supported arches
        all_arches = settings["allowed_arches"]

        # Packages
        log("")
        log("=====  Analyzing Repos & Packages =====")
        log("")
        data["repos"] = {}
        for _,repo in configs["repos"].items():
            repo_id = repo["id"]
            data["pkgs"][repo_id] = {}
            data["repos"][repo_id] = {}
            for arch in repo["source"]["architectures"]:
                data["pkgs"][repo_id][arch] = _analyze_pkgs(tmp_dnf_cachedir, tmp_installroots, repo, arch)
            
            # Reading the optional composeinfo
            data["repos"][repo_id]["compose_date"] = None
            data["repos"][repo_id]["compose_days_ago"] = 0
            if repo["source"]["composeinfo"]:
                # At this point, this is all I can do. Hate me or not, it gets us
                # what we need and won't brake anything in case things go badly. 
                try:
                    with urllib.request.urlopen(repo["source"]["composeinfo"]) as response:
                        composeinfo_raw_response = response.read()

                    composeinfo_data = json.loads(composeinfo_raw_response)
                    data["repos"][repo_id]["composeinfo"] = composeinfo_data

                    compose_date = datetime.datetime.strptime(composeinfo_data["payload"]["compose"]["date"], "%Y%m%d").date()
                    data["repos"][repo_id]["compose_date"] = compose_date.strftime("%Y-%m-%d")

                    date_now = datetime.datetime.now().date()
                    data["repos"][repo_id]["compose_days_ago"] = (date_now - compose_date).days

                except:
                    pass

                

        # Environments
        log("")
        log("=====  Analyzing Environments =====")
        log("")
        data["envs"] = _analyze_envs(tmp_dnf_cachedir, tmp_installroots, configs)

        # Workloads
        log("")
        log("=====  Analyzing Workloads =====")
        log("")
        data["workloads"] = _analyze_workloads(tmp_dnf_cachedir, tmp_installroots, configs, data)


    return data


###############################################################################
### Query gives an easy access to the data! ###################################
###############################################################################

class Query():
    def __init__(self, data, configs, settings):
        self.data = data
        self.configs = configs
        self.settings = settings

    def size(self, num, suffix='B'):
        for unit in ['','k','M','G']:
            if abs(num) < 1024.0:
                return "%3.1f %s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%.1f %s%s" % (num, 'T', suffix)
        

    @lru_cache(maxsize = None)
    def workloads(self, workload_conf_id, env_conf_id, repo_id, arch, list_all=False, output_change=None):
        # accepts none in any argument, and in those cases, answers for all instances

        # It can output just one part of the id.
        # That's useful to, for example, list all arches associated with a workload_conf_id
        if output_change:
            list_all = True
            if output_change not in ["workload_conf_ids", "env_conf_ids", "repo_ids", "arches"]:
                raise ValueError('output_change must be one of: "workload_conf_ids", "env_conf_ids", "repo_ids", "arches"')

        matching_ids = set()

        # list considered workload_conf_ids
        if workload_conf_id:
            workload_conf_ids = [workload_conf_id]
        else:
            workload_conf_ids = self.configs["workloads"].keys()

        # list considered env_conf_ids
        if env_conf_id:
            env_conf_ids = [env_conf_id]
        else:
            env_conf_ids = self.configs["envs"].keys()
        
        # list considered repo_ids
        if repo_id:
            repo_ids = [repo_id]
        else:
            repo_ids = self.configs["repos"].keys()
            
        # list considered arches
        if arch:
            arches = [arch]
        else:
            arches = self.settings["allowed_arches"]
        
        # And now try looping through all of that, and return True on a first occurance
        # This is a terrible amount of loops. But most cases will have just one item
        # in most of those, anyway. No one is expected to run this method with
        # a "None" for every argument!
        for workload_conf_id in workload_conf_ids:
            for env_conf_id in env_conf_ids:
                for repo_id in repo_ids:
                    for arch in arches:
                        workload_id = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
                            workload_conf_id=workload_conf_id,
                            env_conf_id=env_conf_id,
                            repo_id=repo_id,
                            arch=arch
                        )
                        if workload_id in self.data["workloads"].keys():
                            if not list_all:
                                return True
                            if output_change:
                                if output_change == "workload_conf_ids":
                                    matching_ids.add(workload_conf_id)
                                if output_change == "env_conf_ids":
                                    matching_ids.add(env_conf_id)
                                if output_change == "repo_ids":
                                    matching_ids.add(repo_id)
                                if output_change == "arches":
                                    matching_ids.add(arch)
                            else:
                                matching_ids.add(workload_id)
        
        if not list_all:
            return False
        return sorted(list(matching_ids))
    
    @lru_cache(maxsize = None)
    def workloads_id(self, id, list_all=False, output_change=None):
        # Accepts both env and workload ID, and returns workloads that match that
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workloads(None, env_conf_id, repo_id, arch, list_all, output_change)
        
        # It's a workload! Why would you want that, anyway?!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all, output_change)
        
        raise ValueError("That seems to be an invalid ID!")

    @lru_cache(maxsize = None)
    def envs(self, env_conf_id, repo_id, arch, list_all=False, output_change=None):
        # accepts none in any argument, and in those cases, answers for all instances

        # It can output just one part of the id.
        # That's useful to, for example, list all arches associated with a workload_conf_id
        if output_change:
            list_all = True
            if output_change not in ["env_conf_ids", "repo_ids", "arches"]:
                raise ValueError('output_change must be one of: "env_conf_ids", "repo_ids", "arches"')
        
        matching_ids = set()

        # list considered env_conf_ids
        if env_conf_id:
            env_conf_ids = [env_conf_id]
        else:
            env_conf_ids = self.configs["envs"].keys()
        
        # list considered repo_ids
        if repo_id:
            repo_ids = [repo_id]
        else:
            repo_ids = self.configs["repos"].keys()
            
        # list considered arches
        if arch:
            arches = [arch]
        else:
            arches = self.settings["allowed_arches"]
        
        # And now try looping through all of that, and return True on a first occurance
        # This is a terrible amount of loops. But most cases will have just one item
        # in most of those, anyway. No one is expected to run this method with
        # a "None" for every argument!
        for env_conf_id in env_conf_ids:
            for repo_id in repo_ids:
                for arch in arches:
                    env_id = "{env_conf_id}:{repo_id}:{arch}".format(
                        env_conf_id=env_conf_id,
                        repo_id=repo_id,
                        arch=arch
                    )
                    if env_id in self.data["envs"].keys():
                        if not list_all:
                            return True
                        if output_change:
                            if output_change == "env_conf_ids":
                                matching_ids.add(env_conf_id)
                            if output_change == "repo_ids":
                                matching_ids.add(repo_id)
                            if output_change == "arches":
                                matching_ids.add(arch)
                        else:
                            matching_ids.add(env_id)
        
        # This means nothing has been found!
        if not list_all:
            return False
        return sorted(list(matching_ids))
    
    @lru_cache(maxsize = None)
    def envs_id(self, id, list_all=False, output_change=None):
        # Accepts both env and workload ID, and returns workloads that match that
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.envs(env_conf_id, repo_id, arch, list_all, output_change)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.envs(env_conf_id, repo_id, arch, list_all, output_change)
        
        raise ValueError("That seems to be an invalid ID!")
    
    @lru_cache(maxsize = None)
    def workload_pkgs(self, workload_conf_id, env_conf_id, repo_id, arch, output_change=None):
        # Warning: mixing repos and arches works, but might cause mess on the output

        # Default output is just a flat list. Extra fields will be added into each package:
        # q_in          - set of workload_ids including this pkg
        # q_required_in - set of workload_ids where this pkg is required (top-level)
        # q_env_in      - set of workload_ids where this pkg is in env
        # q_arch        - architecture

        # Other outputs:
        #   - "ids"         — a list ids
        #   - "binary_names"  — a list of RPM names
        #   - "source_nvr"  — a list of SRPM NVRs
        #   - "source_names"  — a list of SRPM names
        if output_change:
            list_all = True
            if output_change not in ["ids", "binary_names", "source_nvr", "source_names"]:
                raise ValueError('output_change must be one of: "ids", "binary_names", "source_nvr", "source_names"')
        
        # Step 1: get all the matching workloads!
        workload_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all=True)

        # I'll need repo_ids and arches to access the packages
        repo_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, output_change="repo_ids")
        arches = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, output_change="arches")

        # Replicating the same structure as in data["pkgs"]
        # That is: [repo_id][arch][pkg_id]
        pkgs = {}
        for repo_id in repo_ids:
            pkgs[repo_id] = {}
            for arch in arches:
                pkgs[repo_id][arch] = {}

        # Workloads are already paired with envs, repos, and arches
        # (there is one for each combination)
        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_arch = workload["arch"]
            workload_repo_id = workload["repo_id"]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            # First, get all pkgs in the env
            for pkg_id in workload["pkg_env_ids"]:

                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][workload_repo_id][workload_arch][pkg_id]
                if pkg_id not in pkgs[workload_repo_id][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id] = {}
                    pkgs[workload_repo_id][workload_arch][pkg_id]["id"] = pkg_id
                    pkgs[workload_repo_id][workload_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_arch"] = workload_arch
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"] = set()
                
                # It's here, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"].add(workload_id)
                # Browsing env packages, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"].add(workload_id)
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
            
            # Second, add all the other packages
            for pkg_id in workload["pkg_added_ids"]:

                # Add it to the list if it's not there already
                # and initialize extra fields
                pkg = self.data["pkgs"][workload_repo_id][workload_arch][pkg_id]
                if pkg_id not in pkgs[workload_repo_id][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id] = {}
                    pkgs[workload_repo_id][workload_arch][pkg_id]["id"] = pkg_id
                    pkgs[workload_repo_id][workload_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_arch"] = workload_arch
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"] = set()
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_env_in"] = set()
                
                # It's here, so add it
                pkgs[workload_repo_id][workload_arch][pkg_id]["q_in"].add(workload_id)
                # Not adding it to q_env_in
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][pkg_id]["q_required_in"].add(workload_id)
            
            # Third, add package placeholders if any
            for placeholder_id in workload["pkg_placeholder_ids"]:
                placeholder = workload_conf["package_placeholders"][pkg_id_to_name(placeholder_id)]
                if placeholder_id not in pkgs[workload_repo_id][workload_arch]:
                    pkgs[workload_repo_id][workload_arch][placeholder_id] = {}
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["id"] = placeholder_id
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["name"] = placeholder["name"]
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["evr"] = "000-placeholder"
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["arch"] = "placeholder"
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["installsize"] = 0
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["description"] = placeholder["description"]
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["summary"] = placeholder["description"]
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["source_name"] = placeholder["srpm"]
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["q_arch"] = workload_arch
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["q_in"] = set()
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["q_required_in"] = set()
                    pkgs[workload_repo_id][workload_arch][placeholder_id]["q_env_in"] = set()

                # It's here, so add it
                pkgs[workload_repo_id][workload_arch][placeholder_id]["q_in"].add(workload_id)
                # All placeholders are required
                pkgs[workload_repo_id][workload_arch][placeholder_id]["q_required_in"].add(workload_id)

        # Is it supposed to only output ids?
        if output_change:
            pkg_names = set()
            for repo_id in repo_ids:
                for arch in arches:
                    for pkg_id, pkg in pkgs[repo_id][arch].items():
                        if output_change == "ids":
                            pkg_names.add(pkg["id"])
                        elif output_change == "binary_names":
                            pkg_names.add(pkg["name"])
                        elif output_change == "source_nvr":
                            pkg_names.add(pkg["sourcerpm"])
                        elif output_change == "source_names":
                            pkg_names.add(pkg["source_name"])
            
            names_sorted = sorted(list(pkg_names))
            return names_sorted
                        

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for repo_id in repo_ids:
            for arch in arches:
                for pkg_id, pkg in pkgs[repo_id][arch].items():
                    final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted


    @lru_cache(maxsize = None)
    def workload_pkgs_id(self, id, output_change=None):
        # Accepts both env and workload ID, and returns pkgs for workloads that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workload_pkgs(None, env_conf_id, repo_id, arch, output_change)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workload_pkgs(workload_conf_id, env_conf_id, repo_id, arch, output_change)
        
        raise ValueError("That seems to be an invalid ID!")
    
    @lru_cache(maxsize = None)
    def env_pkgs(self, env_conf_id, repo_id, arch):
        # Warning: mixing repos and arches works, but might cause mess on the output

        # Output is just a flat list. Extra fields will be added into each package:
        # q_in          - set of env_ids including this pkg
        # q_required_in - set of env_ids where this pkg is required (top-level)
        # q_arch        - architecture

        
        # Step 1: get all the matching envs!
        env_ids = self.envs(env_conf_id, repo_id, arch, list_all=True)

        # I'll need repo_ids and arches to access the packages
        repo_ids = self.envs(env_conf_id, repo_id, arch, output_change="repo_ids")
        arches = self.envs(env_conf_id, repo_id, arch, output_change="arches")

        # Replicating the same structure as in data["pkgs"]
        # That is: [repo_id][arch][pkg_id]
        pkgs = {}
        for repo_id in repo_ids:
            pkgs[repo_id] = {}
            for arch in arches:
                pkgs[repo_id][arch] = {}

        # envs are already paired with repos, and arches
        # (there is one for each combination)
        for env_id in env_ids:
            env = self.data["envs"][env_id]
            env_arch = env["arch"]
            env_repo_id = env["repo_id"]
            env_conf_id = env["env_conf_id"]

            for pkg_id in env["pkg_ids"]:

                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][env_repo_id][env_arch][pkg_id]
                if pkg_id not in pkgs[env_repo_id][env_arch]:
                    pkgs[env_repo_id][env_arch][pkg_id] = {}
                    pkgs[env_repo_id][env_arch][pkg_id]["id"] = pkg_id
                    pkgs[env_repo_id][env_arch][pkg_id]["name"] = pkg["name"]
                    pkgs[env_repo_id][env_arch][pkg_id]["evr"] = pkg["evr"]
                    pkgs[env_repo_id][env_arch][pkg_id]["arch"] = pkg["arch"]
                    pkgs[env_repo_id][env_arch][pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[env_repo_id][env_arch][pkg_id]["description"] = pkg["description"]
                    pkgs[env_repo_id][env_arch][pkg_id]["summary"] = pkg["summary"]
                    pkgs[env_repo_id][env_arch][pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[env_repo_id][env_arch][pkg_id]["sourcerpm"] = pkg["sourcerpm"]
                    pkgs[env_repo_id][env_arch][pkg_id]["q_arch"] = env_arch
                    pkgs[env_repo_id][env_arch][pkg_id]["q_in"] = set()
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"] = set()
                
                # It's here, so add it
                pkgs[env_repo_id][env_arch][pkg_id]["q_in"].add(env_id)
                # Is it required?
                if pkg["name"] in self.configs["envs"][env_conf_id]["packages"]:
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"].add(env_id)
                if pkg["name"] in self.configs["envs"][env_conf_id]["arch_packages"][env_arch]:
                    pkgs[env_repo_id][env_arch][pkg_id]["q_required_in"].add(env_id)

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for repo_id in repo_ids:
            for arch in arches:
                for pkg_id, pkg in pkgs[repo_id][arch].items():
                    final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted
    
    @lru_cache(maxsize = None)
    def env_pkgs_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.env_pkgs(env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.env_pkgs(env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")

    @lru_cache(maxsize = None)
    def workload_size(self, workload_conf_id, env_conf_id, repo_id, arch):
        # A total size of a workload (or multiple combined!)
        pkgs = self.workload_pkgs(workload_conf_id, env_conf_id, repo_id, arch)
        size = 0
        for pkg in pkgs:
            size += pkg["installsize"]
        return size

    @lru_cache(maxsize = None)
    def env_size(self, env_conf_id, repo_id, arch):
        # A total size of an env (or multiple combined!)
        pkgs = self.env_pkgs(env_conf_id, repo_id, arch)
        size = 0
        for pkg in pkgs:
            size += pkg["installsize"]
        return size

    @lru_cache(maxsize = None)
    def workload_size_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.workload_size(None, env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.workload_size(workload_conf_id, env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")
    
    @lru_cache(maxsize = None)
    def env_size_id(self, id):
        # Accepts both env and workload ID, and returns pkgs for envs that match
        id_components = id.split(":")

        # It's an env!
        if len(id_components) == 3:
            env_conf_id = id_components[0]
            repo_id = id_components[1]
            arch = id_components[2]
            return self.env_size(env_conf_id, repo_id, arch)
        
        # It's a workload!
        if len(id_components) == 4:
            workload_conf_id = id_components[0]
            env_conf_id = id_components[1]
            repo_id = id_components[2]
            arch = id_components[3]
            return self.env_size(env_conf_id, repo_id, arch)
        
        raise ValueError("That seems to be an invalid ID!")
    
    def workload_url_slug(self, workload_conf_id, env_conf_id, repo_id, arch):
        slug = "{workload_conf_id}--{env_conf_id}--{repo_id}--{arch}".format(
            workload_conf_id=workload_conf_id,
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def env_url_slug(self, env_conf_id, repo_id, arch):
        slug = "{env_conf_id}--{repo_id}--{arch}".format(
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug

    def workload_id_string(self, workload_conf_id, env_conf_id, repo_id, arch):
        slug = "{workload_conf_id}:{env_conf_id}:{repo_id}:{arch}".format(
            workload_conf_id=workload_conf_id,
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def env_id_string(self, env_conf_id, repo_id, arch):
        slug = "{env_conf_id}:{repo_id}:{arch}".format(
            env_conf_id=env_conf_id,
            repo_id=repo_id,
            arch=arch
        )
        return slug
    
    def url_slug_id(self, any_id):
        return any_id.replace(":", "--")
    
    @lru_cache(maxsize = None)
    def workloads_in_view(self, view_conf_id, arch, maintainer=None):
        view_conf = self.configs["views"][view_conf_id]
        repo_id = view_conf["repository"]
        labels = view_conf["labels"]
        
        if arch and arch not in self.settings["allowed_arches"]:
            raise ValueError("Unsupported arch: {arch}".format(
                arch=arch
            ))
        
        if arch and arch not in self.arches_in_view(view_conf_id):
            return []

        # First, get a set of workloads matching the repo and the arch
        too_many_workload_ids = set()
        workload_ids = self.workloads(None,None,repo_id,arch,list_all=True)
        too_many_workload_ids.update(workload_ids)

        # Second, limit that set further by matching the label
        final_workload_ids = set()
        for workload_id in too_many_workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            if maintainer:
                workload_maintainer = workload_conf["maintainer"]
                if workload_maintainer != maintainer:
                    continue

            workload_labels = workload_conf["labels"]
            for workload_label in workload_labels:
                if workload_label in labels:
                    final_workload_ids.add(workload_id)

        return sorted(list(final_workload_ids))
    
    @lru_cache(maxsize = None)
    def arches_in_view(self, view_conf_id, maintainer=None):

        if len(self.configs["views"][view_conf_id]["architectures"]):
            arches = self.configs["views"][view_conf_id]["architectures"]
            return sorted(arches)
        
        return self.settings["allowed_arches"]
    
    @lru_cache(maxsize = None)
    def pkgs_in_view(self, view_conf_id, arch, output_change=None, maintainer=None):

        # Extra fields will be added into each package:
        # q_in          - set of workload_ids including this pkg
        # q_required_in - set of workload_ids where this pkg is required (top-level)
        # q_env_in      - set of workload_ids where this pkg is in env
        # q_dep_in      - set of workload_ids where this pkg is a dependency (that means not required)
        # q_maintainers - set of workload maintainers 

        # Other outputs:
        #   - "ids"         — a list of ids (NEVRA)
        #   - "nevrs"         — a list of NEVR
        #   - "binary_names"  — a list of RPM names
        #   - "source_nvr"  — a list of SRPM NVRs
        #   - "source_names"  — a list of SRPM names
        if output_change:
            list_all = True
            if output_change not in ["ids", "nevrs", "binary_names", "source_nvr", "source_names"]:
                raise ValueError('output_change must be one of: "ids", "nevrs", "binary_names", "source_nvr", "source_names"')

        workload_ids = self.workloads_in_view(view_conf_id, arch)
        repo_id = self.configs["views"][view_conf_id]["repository"]

        # This has just one repo and one arch, so a flat list of IDs is enough
        pkgs = {}
        
        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            # First, get all pkgs in the env
            for pkg_id in workload["pkg_env_ids"]:
                # Add it to the list if it's not there already.
                # Create a copy since it's gonna be modified, and include only what's needed
                pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                if pkg_id not in pkgs:
                    pkgs[pkg_id] = {}
                    pkgs[pkg_id]["id"] = pkg_id
                    pkgs[pkg_id]["name"] = pkg["name"]
                    pkgs[pkg_id]["evr"] = pkg["evr"]
                    pkgs[pkg_id]["arch"] = pkg["arch"]
                    pkgs[pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[pkg_id]["description"] = pkg["description"]
                    pkgs[pkg_id]["summary"] = pkg["summary"]
                    pkgs[pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[pkg_id]["sourcerpm"] = pkg["sourcerpm"]
                    pkgs[pkg_id]["q_arch"] = arch
                    pkgs[pkg_id]["q_in"] = set()
                    pkgs[pkg_id]["q_required_in"] = set()
                    pkgs[pkg_id]["q_dep_in"] = set()
                    pkgs[pkg_id]["q_env_in"] = set()
                    pkgs[pkg_id]["q_maintainers"] = set()
                
                # It's here, so add it
                pkgs[pkg_id]["q_in"].add(workload_id)
                # Browsing env packages, so add it
                pkgs[pkg_id]["q_env_in"].add(workload_id)
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][arch]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)

            # Second, add all the other packages
            for pkg_id in workload["pkg_added_ids"]:

                # Add it to the list if it's not there already
                # and initialize extra fields
                pkg = self.data["pkgs"][repo_id][arch][pkg_id]
                if pkg_id not in pkgs:
                    pkgs[pkg_id] = {}
                    pkgs[pkg_id]["id"] = pkg_id
                    pkgs[pkg_id]["name"] = pkg["name"]
                    pkgs[pkg_id]["evr"] = pkg["evr"]
                    pkgs[pkg_id]["arch"] = pkg["arch"]
                    pkgs[pkg_id]["installsize"] = pkg["installsize"]
                    pkgs[pkg_id]["description"] = pkg["description"]
                    pkgs[pkg_id]["summary"] = pkg["summary"]
                    pkgs[pkg_id]["source_name"] = pkg["source_name"]
                    pkgs[pkg_id]["sourcerpm"] = pkg["sourcerpm"]
                    pkgs[pkg_id]["q_arch"] = arch
                    pkgs[pkg_id]["q_in"] = set()
                    pkgs[pkg_id]["q_required_in"] = set()
                    pkgs[pkg_id]["q_dep_in"] = set()
                    pkgs[pkg_id]["q_env_in"] = set()
                    pkgs[pkg_id]["q_maintainers"] = set()
                
                # It's here, so add it
                pkgs[pkg_id]["q_in"].add(workload_id)
                # Not adding it to q_env_in
                # Is it required?
                if pkg["name"] in self.configs["workloads"][workload_conf_id]["packages"]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                elif pkg["name"] in self.configs["workloads"][workload_conf_id]["arch_packages"][arch]:
                    pkgs[pkg_id]["q_required_in"].add(workload_id)
                else:
                    pkgs[pkg_id]["q_dep_in"].add(workload_id)
                # Maintainer
                pkgs[pkg_id]["q_maintainers"].add(workload_conf["maintainer"])

            # Third, add package placeholders if any
            for placeholder_id in workload["pkg_placeholder_ids"]:
                placeholder = workload_conf["package_placeholders"][pkg_id_to_name(placeholder_id)]
                if placeholder_id not in pkgs:
                    pkgs[placeholder_id] = {}
                    pkgs[placeholder_id]["id"] = placeholder_id
                    pkgs[placeholder_id]["name"] = placeholder["name"]
                    pkgs[placeholder_id]["evr"] = "000-placeholder"
                    pkgs[placeholder_id]["arch"] = "placeholder"
                    pkgs[placeholder_id]["installsize"] = 0
                    pkgs[placeholder_id]["description"] = placeholder["description"]
                    pkgs[placeholder_id]["summary"] = placeholder["description"]
                    pkgs[placeholder_id]["source_name"] = placeholder["srpm"]
                    pkgs[placeholder_id]["sourcerpm"] = "{}-000-placeholder".format(placeholder["srpm"])
                    pkgs[placeholder_id]["q_arch"] = arch
                    pkgs[placeholder_id]["q_in"] = set()
                    pkgs[placeholder_id]["q_required_in"] = set()
                    pkgs[placeholder_id]["q_dep_in"] = set()
                    pkgs[placeholder_id]["q_env_in"] = set()
                    pkgs[placeholder_id]["q_maintainers"] = set()
                
                # It's here, so add it
                pkgs[placeholder_id]["q_in"].add(workload_id)
                # All placeholders are required
                pkgs[placeholder_id]["q_required_in"].add(workload_id)
                # Maintainer
                pkgs[placeholder_id]["q_maintainers"].add(workload_conf["maintainer"])
                

        # Filtering by a maintainer?
        # Filter out packages not belonging to the maintainer
        # It's filtered out at this stage to keep the context of fields like
        # "q_required_in" etc. to be the whole view
        pkg_ids_to_delete = set()
        if maintainer:
            for pkg_id, pkg in pkgs.items():
                if maintainer not in pkg["q_maintainers"]:
                    pkg_ids_to_delete.add(pkg_id)
        for pkg_id in pkg_ids_to_delete:
            del pkgs[pkg_id]

        # Is it supposed to only output ids?
        if output_change:
            pkg_names = set()
            for pkg_id, pkg in pkgs.items():
                if output_change == "ids":
                    pkg_names.add(pkg["id"])
                elif output_change == "nevrs":
                    pkg_names.add("{name}-{evr}".format(
                        name=pkg["name"],
                        evr=pkg["evr"]
                    ))
                elif output_change == "binary_names":
                    pkg_names.add(pkg["name"])
                elif output_change == "source_nvr":
                    pkg_names.add(pkg["sourcerpm"])
                elif output_change == "source_names":
                    pkg_names.add(pkg["source_name"])
            
            names_sorted = sorted(list(pkg_names))
            return names_sorted
                        

        # And now I just need to flatten that dict and return all packages as a list
        final_pkg_list = []
        for pkg_id, pkg in pkgs.items():
            final_pkg_list.append(pkg)

        # And sort them by nevr which is their ID
        final_pkg_list_sorted = sorted(final_pkg_list, key=lambda k: k['id'])

        return final_pkg_list_sorted
    

    @lru_cache(maxsize = None)
    def view_buildroot_pkgs(self, view_conf_id, arch, output_change=None, maintainer=None):
        # Other outputs:
        #   - "source_names"  — a list of SRPM names
        if output_change:
            if output_change not in ["source_names"]:
                raise ValueError('output_change must be one of: "source_names"')

        pkgs = {}

        buildroot_conf_id = None
        for conf_id, conf in self.configs["buildroots"].items():
            if conf["view_id"] == view_conf_id:
                buildroot_conf_id = conf_id

        if not buildroot_conf_id:
            if output_change == "source_names":
                return []
            return {}

        # Populate pkgs

        base_buildroot = self.configs["buildroots"][buildroot_conf_id]["base_buildroot"][arch]
        source_pkgs = self.configs["buildroots"][buildroot_conf_id]["source_packages"][arch]

        for pkg_name in base_buildroot:
            if pkg_name not in pkgs:
                pkgs[pkg_name] = {}
                pkgs[pkg_name]["required_by"] = set()
                pkgs[pkg_name]["base_buildroot"] = True
                pkgs[pkg_name]["srpm_name"] = None

        for srpm_name, srpm_data in source_pkgs.items():
            for pkg_name in srpm_data["requires"]:
                if pkg_name not in pkgs:
                    pkgs[pkg_name] = {}
                    pkgs[pkg_name]["required_by"] = set()
                    pkgs[pkg_name]["base_buildroot"] = False
                    pkgs[pkg_name]["srpm_name"] = None
                pkgs[pkg_name]["required_by"].add(srpm_name)

        for buildroot_pkg_relations_conf_id, buildroot_pkg_relations_conf in self.configs["buildroot_pkg_relations"].items():
            if view_conf_id != buildroot_pkg_relations_conf["view_id"]:
                continue

            if arch != buildroot_pkg_relations_conf["arch"]:
                continue
        
            buildroot_pkg_relations = buildroot_pkg_relations_conf["pkg_relations"]

            for this_pkg_id in buildroot_pkg_relations:
                this_pkg_name = pkg_id_to_name(this_pkg_id)

                if this_pkg_name in pkgs:

                    if this_pkg_id in buildroot_pkg_relations and not pkgs[this_pkg_name]["srpm_name"]:
                        pkgs[this_pkg_name]["srpm_name"] = buildroot_pkg_relations[this_pkg_id]["source_name"]


        if output_change == "source_names":
            srpms = set()

            for pkg_name, pkg in pkgs.items():
                if pkg["srpm_name"]:
                    srpms.add(pkg["srpm_name"])

            srpm_names_sorted = sorted(list(srpms))
            return srpm_names_sorted
        
        return pkgs
    
    
    @lru_cache(maxsize = None)
    def workload_succeeded(self, workload_conf_id, env_conf_id, repo_id, arch):
        workload_ids = self.workloads(workload_conf_id, env_conf_id, repo_id, arch, list_all=True)

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            if not workload["succeeded"]:
                return False
        return True
    
    @lru_cache(maxsize = None)
    def env_succeeded(self, env_conf_id, repo_id, arch):
        env_ids = self.envs(env_conf_id, repo_id, arch, list_all=True)

        for env_id in env_ids:
            env = self.data["envs"][env_id]
            if not env["succeeded"]:
                return False
        return True
    
    @lru_cache(maxsize = None)
    def view_succeeded(self, view_conf_id, arch, maintainer=None):
        workload_ids = self.workloads_in_view(view_conf_id, arch)

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            if maintainer:
                workload_maintainer = workload_conf["maintainer"]
                if workload_maintainer != maintainer:
                    continue

            if not workload["succeeded"]:
                return False
        return True
    

    def _srpm_name_to_rpm_names(self, srpm_name, repo_id):
        all_pkgs_by_arch = self.data["pkgs"][repo_id]

        pkg_names = set()

        for arch, pkgs in all_pkgs_by_arch.items():
            for pkg_id, pkg in pkgs.items():
                if pkg["source_name"] == srpm_name:
                    pkg_names.add(pkg["name"])

        return pkg_names

    
    @lru_cache(maxsize = None)
    def view_unwanted_pkgs(self, view_conf_id, arch, output_change=None, maintainer=None):

        # Other outputs:
        #   - "unwanted_proposals"  — a list of SRPM names
        #   - "unwanted_confirmed"  — a list of SRPM names
        output_lists = ["unwanted_proposals", "unwanted_confirmed"]
        if output_change:
            if output_change not in output_lists:
                raise ValueError('output_change must be one of: "source_names"')
        
            output_lists = output_change


        view_conf = self.configs["views"][view_conf_id]
        repo_id = view_conf["repository"]

        # Find exclusion lists mathing this view's label(s)
        unwanted_ids = set()
        for view_label in view_conf["labels"]:
            for unwanted_id, unwanted in self.configs["unwanteds"].items():
                if maintainer:
                    unwanted_maintainer = unwanted["maintainer"]
                    if unwanted_maintainer != maintainer:
                        continue
                for unwanted_label in unwanted["labels"]:
                    if view_label == unwanted_label:
                        unwanted_ids.add(unwanted_id)
        
        # This will be the package list
        unwanted_pkg_names = {}

        arches = self.settings["allowed_arches"]
        if arch:
            arches = [arch]

        ### Step 1: Get packages from this view's config (unwanted confirmed)
        if "unwanted_confirmed" in output_lists:
            if not maintainer:
                for pkg_name in view_conf["unwanted_packages"]:
                    pkg = {}
                    pkg["name"] = pkg_name
                    pkg["unwanted_in_view"] = True
                    pkg["unwanted_list_ids"] = []

                    unwanted_pkg_names[pkg_name] = pkg

                for arch in arches:
                    for pkg_name in view_conf["unwanted_arch_packages"][arch]:
                        if pkg_name in unwanted_pkg_names:
                            continue
                        
                        pkg = {}
                        pkg["name"] = pkg_name
                        pkg["unwanted_in_view"] = True
                        pkg["unwanted_list_ids"] = []

                        unwanted_pkg_names[pkg_name] = pkg
                
                for pkg_source_name in view_conf["unwanted_source_packages"]:
                    for pkg_name in self._srpm_name_to_rpm_names(pkg_source_name, repo_id):
                        
                        if pkg_name in unwanted_pkg_names:
                            continue

                        pkg = {}
                        pkg["name"] = pkg_name
                        pkg["unwanted_in_view"] = True
                        pkg["unwanted_list_ids"] = []

                        unwanted_pkg_names[pkg_name] = pkg


        ### Step 2: Get packages from the various exclusion lists (unwanted proposal)
        if "unwanted_proposals" in output_lists:
            for unwanted_id in unwanted_ids:
                unwanted_conf = self.configs["unwanteds"][unwanted_id]

                for pkg_name in unwanted_conf["unwanted_packages"]:
                    if pkg_name in unwanted_pkg_names:
                        unwanted_pkg_names[pkg_name]["unwanted_list_ids"].append(unwanted_id)
                        continue
                    
                    pkg = {}
                    pkg["name"] = pkg_name
                    pkg["unwanted_in_view"] = False
                    pkg["unwanted_list_ids"] = [unwanted_id]

                    unwanted_pkg_names[pkg_name] = pkg
            
                for arch in arches:
                    for pkg_name in unwanted_conf["unwanted_arch_packages"][arch]:
                        if pkg_name in unwanted_pkg_names:
                            unwanted_pkg_names[pkg_name]["unwanted_list_ids"].append(unwanted_id)
                            continue
                        
                        pkg = {}
                        pkg["name"] = pkg_name
                        pkg["unwanted_in_view"] = True
                        pkg["unwanted_list_ids"] = []

                        unwanted_pkg_names[pkg_name] = pkg
                
                for pkg_source_name in unwanted_conf["unwanted_source_packages"]:
                    for pkg_name in self._srpm_name_to_rpm_names(pkg_source_name, repo_id):

                        if pkg_name in unwanted_pkg_names:
                            unwanted_pkg_names[pkg_name]["unwanted_list_ids"].append(unwanted_id)
                            continue
                        
                        pkg = {}
                        pkg["name"] = pkg_name
                        pkg["unwanted_in_view"] = False
                        pkg["unwanted_list_ids"] = [unwanted_id]

                        unwanted_pkg_names[pkg_name] = pkg

        #self.cache["view_unwanted_pkgs"][view_conf_id][arch] = unwanted_pkg_names

        return unwanted_pkg_names


    @lru_cache(maxsize = None)
    def view_placeholder_srpms(self, view_conf_id, arch):
        if not arch:
            raise ValueError("arch must be specified, can't be None")

        workload_ids = self.workloads_in_view(view_conf_id, arch)

        placeholder_srpms = {}
        # {
        #    "SRPM_NAME": {
        #        "build_requires": set() 
        #    } 
        # } 

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            for pkg_placeholder_name, pkg_placeholder in workload_conf["package_placeholders"].items():
                # Placeholders can be limited to specific architectures.
                # If that's the case, check if it's available on this arch, otherwise skip it.
                if pkg_placeholder["limit_arches"]:
                    if arch not in pkg_placeholder["limit_arches"]:
                        continue

                # SRPM is optional. 
                srpm_name = pkg_placeholder["srpm"]
                if not srpm_name:
                    continue

                buildrequires = pkg_placeholder["buildrequires"]

                if srpm_name not in placeholder_srpms:
                    placeholder_srpms[srpm_name] = {}
                    placeholder_srpms[srpm_name]["build_requires"] = set()
                
                placeholder_srpms[srpm_name]["build_requires"].update(buildrequires)
        
        return placeholder_srpms


    @lru_cache(maxsize = None)
    def view_modules(self, view_conf_id, arch, maintainer=None):
        workload_ids = self.workloads_in_view(view_conf_id, arch, maintainer)

        modules = {}

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]

            required_modules = workload_conf["modules_enable"]

            for module_id in workload["enabled_modules"]:
                if module_id not in modules:
                    modules[module_id] = {}
                    modules[module_id]["id"] = module_id
                    modules[module_id]["q_in"] = set()
                    modules[module_id]["q_required_in"] = set()
                    modules[module_id]["q_dep_in"] = set()
                
                modules[module_id]["q_in"].add(workload_id)

                if module_id in required_modules:
                    modules[module_id]["q_required_in"].add(workload_id)
                else:
                    modules[module_id]["q_dep_in"].add(workload_id)
                

        return modules


    @lru_cache(maxsize = None)
    def view_maintainers(self, view_conf_id, arch):
        workload_ids = self.workloads_in_view(view_conf_id, arch)

        maintainers = set()

        for workload_id in workload_ids:
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]
            maintainers.add(workload_conf["maintainer"])

        return maintainers


    @lru_cache(maxsize = None)
    def maintainers(self):

        maintainers = {}

        for workload_id in self.workloads(None, None, None, None, list_all=True):
            workload = self.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.configs["workloads"][workload_conf_id]
            maintainer = workload_conf["maintainer"]

            if maintainer not in maintainers:
                maintainers[maintainer] = {}
                maintainers[maintainer]["name"] = maintainer
                maintainers[maintainer]["all_succeeded"] = True
            
            if not workload["succeeded"]:
                maintainers[maintainer]["all_succeeded"] = False

        for env_id in self.envs(None, None, None, list_all=True):
            env = self.data["envs"][env_id]
            env_conf_id = env["env_conf_id"]
            env_conf = self.configs["envs"][env_conf_id]
            maintainer = env_conf["maintainer"]

            if maintainer not in maintainers:
                maintainers[maintainer] = {}
                maintainers[maintainer]["name"] = maintainer
                maintainers[maintainer]["all_succeeded"] = True
            
            if not env["succeeded"]:
                maintainers[maintainer]["all_succeeded"] = False

        return maintainers
    

    @lru_cache(maxsize = None)
    def view_pkg_name_details(self, pkg_name, view_conf_id):
        raise NotImplementedError

    
    @lru_cache(maxsize = None)
    def view_srpm_name_details(self, srpm_name, view_conf_id):
        raise NotImplementedError
    




###############################################################################
### Generating html pages! ####################################################
###############################################################################


def _generate_html_page(template_name, template_data, page_name, settings):
    log("Generating the '{page_name}' page...".format(
        page_name=page_name
    ))

    output = settings["output"]

    template_loader = jinja2.FileSystemLoader(searchpath="./templates/")
    template_env = jinja2.Environment(loader=template_loader)

    template = template_env.get_template("{template_name}.html".format(
        template_name=template_name
    ))

    if not template_data:
        template_data = {}
    template_data["global_refresh_time_started"] = settings["global_refresh_time_started"]

    page = template.render(**template_data)

    filename = ("{page_name}.html".format(
        page_name=page_name.replace(":", "--")
    ))

    log("  Writing file...  ({filename})".format(
        filename=filename
    ))
    with open(os.path.join(output, filename), "w") as file:
        file.write(page)
    
    log("  Done!")
    log("")


def _generate_json_page(data, page_name, settings):
    log("Generating the '{page_name}' JSON page...".format(
        page_name=page_name
    ))

    output = settings["output"]

    filename = ("{page_name}.json".format(
        page_name=page_name.replace(":", "--")
    ))
    log("  Writing file...  ({filename})".format(
        filename=filename
    ))
    dump_data(os.path.join(output, filename), data)
    
    log("  Done!")
    log("")


def _generate_workload_pages(query):
    log("Generating workload pages...")

    # Workload overview pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            template_data = {
                "query": query,
                "workload_conf_id": workload_conf_id,
                "repo_id": repo_id
            }

            page_name = "workload-overview--{workload_conf_id}--{repo_id}".format(
                workload_conf_id=workload_conf_id,
                repo_id=repo_id
            )
            _generate_html_page("workload_overview", template_data, page_name, query.settings)
    
    # Workload detail pages
    for workload_id in query.workloads(None,None,None,None,list_all=True):
        workload = query.data["workloads"][workload_id]
        
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = query.configs["workloads"][workload_conf_id]

        env_conf_id = workload["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        repo_id = workload["repo_id"]
        repo = query.configs["repos"][repo_id]


        template_data = {
            "query": query,
            "workload_id": workload_id,
            "workload": workload,
            "workload_conf": workload_conf,
            "env_conf": env_conf,
            "repo": repo
        }

        page_name = "workload--{workload_id}".format(
            workload_id=workload_id
        )
        _generate_html_page("workload", template_data, page_name, query.settings)
        page_name = "workload-dependencies--{workload_id}".format(
            workload_id=workload_id
        )
        _generate_html_page("workload_dependencies", template_data, page_name, query.settings)
    
    # Workload compare arches pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for env_conf_id in query.workloads(workload_conf_id,None,None,None,output_change="env_conf_ids"):
            for repo_id in query.workloads(workload_conf_id,env_conf_id,None,None,output_change="repo_ids"):

                arches = query.workloads(workload_conf_id,env_conf_id,repo_id,None,output_change="arches")

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                columns = {}
                rows = set()
                for arch in arches:
                    columns[arch] = {}

                    pkgs = query.workload_pkgs(workload_conf_id,env_conf_id,repo_id,arch)
                    for pkg in pkgs:
                        name = pkg["name"]
                        rows.add(name)
                        columns[arch][name] = pkg

                template_data = {
                    "query": query,
                    "workload_conf_id": workload_conf_id,
                    "workload_conf": workload_conf,
                    "env_conf_id": env_conf_id,
                    "env_conf": env_conf,
                    "repo_id": repo_id,
                    "repo": repo,
                    "columns": columns,
                    "rows": rows
                }

                page_name = "workload-cmp-arches--{workload_conf_id}--{env_conf_id}--{repo_id}".format(
                    workload_conf_id=workload_conf_id,
                    env_conf_id=env_conf_id,
                    repo_id=repo_id
                )

                _generate_html_page("workload_cmp_arches", template_data, page_name, query.settings)
    
    # Workload compare envs pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            for arch in query.workloads(workload_conf_id,None,repo_id,None,output_change="arches"):

                env_conf_ids = query.workloads(workload_conf_id,None,repo_id,arch,output_change="env_conf_ids")

                workload_conf = query.configs["workloads"][workload_conf_id]
                repo = query.configs["repos"][repo_id]

                columns = {}
                rows = set()
                for env_conf_id in env_conf_ids:
                    columns[env_conf_id] = {}

                    pkgs = query.workload_pkgs(workload_conf_id,env_conf_id,repo_id,arch)
                    for pkg in pkgs:
                        name = pkg["name"]
                        rows.add(name)
                        columns[env_conf_id][name] = pkg

                template_data = {
                    "query": query,
                    "workload_conf_id": workload_conf_id,
                    "workload_conf": workload_conf,
                    "repo_id": repo_id,
                    "repo": repo,
                    "arch": arch,
                    "columns": columns,
                    "rows": rows
                }

                page_name = "workload-cmp-envs--{workload_conf_id}--{repo_id}--{arch}".format(
                    workload_conf_id=workload_conf_id,
                    repo_id=repo_id,
                    arch=arch
                )

                _generate_html_page("workload_cmp_envs", template_data, page_name, query.settings)
    
    log("  Done!")
    log("")


def _generate_env_pages(query):
    log("Generating env pages...")

    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):
            template_data = {
                "query": query,
                "env_conf_id": env_conf_id,
                "repo_id": repo_id
            }

            page_name = "env-overview--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _generate_html_page("env_overview", template_data, page_name, query.settings)
    
    # env detail pages
    for env_id in query.envs(None,None,None,list_all=True):
        env = query.data["envs"][env_id]

        env_conf_id = env["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        repo_id = env["repo_id"]
        repo = query.configs["repos"][repo_id]

        template_data = {
            "query": query,
            "env_id": env_id,
            "env": env,
            "env_conf": env_conf,
            "repo": repo
        }

        page_name = "env--{env_id}".format(
            env_id=env_id
        )
        _generate_html_page("env", template_data, page_name, query.settings)

        page_name = "env-dependencies--{env_id}".format(
            env_id=env_id
        )
        _generate_html_page("env_dependencies", template_data, page_name, query.settings)
    
    # env compare arches pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            arches = query.envs(env_conf_id,repo_id,None,output_change="arches")

            env_conf = query.configs["envs"][env_conf_id]
            repo = query.configs["repos"][repo_id]

            columns = {}
            rows = set()
            for arch in arches:
                columns[arch] = {}

                pkgs = query.env_pkgs(env_conf_id,repo_id,arch)
                for pkg in pkgs:
                    name = pkg["name"]
                    rows.add(name)
                    columns[arch][name] = pkg

            template_data = {
                "query": query,
                "env_conf_id": env_conf_id,
                "env_conf": env_conf,
                "repo_id": repo_id,
                "repo": repo,
                "columns": columns,
                "rows": rows
            }

            page_name = "env-cmp-arches--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )

            _generate_html_page("env_cmp_arches", template_data, page_name, query.settings)

    log("  Done!")
    log("")

def _generate_maintainer_pages(query):
    log("Generating maintainer pages...")

    for maintainer in query.maintainers():
    
        template_data = {
            "query": query,
            "maintainer": maintainer
        }

        page_name = "maintainer--{maintainer}".format(
            maintainer=maintainer
        )
        _generate_html_page("maintainer", template_data, page_name, query.settings)

    log("  Done!")
    log("")


def _generate_config_pages(query):
    log("Generating config pages...")

    for conf_type in ["repos", "envs", "workloads", "labels", "views", "unwanteds"]:
        template_data = {
            "query": query,
            "conf_type": conf_type
        }
        page_name = "configs_{conf_type}".format(
            conf_type=conf_type
        )
        _generate_html_page("configs", template_data, page_name, query.settings)

    # Config repo pages
    for repo_id,repo_conf in query.configs["repos"].items():
        template_data = {
            "query": query,
            "repo_conf": repo_conf
        }
        page_name = "config-repo--{repo_id}".format(
            repo_id=repo_id
        )
        _generate_html_page("config_repo", template_data, page_name, query.settings)
    
    # Config env pages
    for env_conf_id,env_conf in query.configs["envs"].items():
        template_data = {
            "query": query,
            "env_conf": env_conf
        }
        page_name = "config-env--{env_conf_id}".format(
            env_conf_id=env_conf_id
        )
        _generate_html_page("config_env", template_data, page_name, query.settings)

    # Config workload pages
    for workload_conf_id,workload_conf in query.configs["workloads"].items():
        template_data = {
            "query": query,
            "workload_conf": workload_conf
        }
        page_name = "config-workload--{workload_conf_id}".format(
            workload_conf_id=workload_conf_id
        )
        _generate_html_page("config_workload", template_data, page_name, query.settings)

    # Config label pages
    for label_conf_id,label_conf in query.configs["labels"].items():
        template_data = {
            "query": query,
            "label_conf": label_conf
        }
        page_name = "config-label--{label_conf_id}".format(
            label_conf_id=label_conf_id
        )
        _generate_html_page("config_label", template_data, page_name, query.settings)

    # Config view pages
    for view_conf_id,view_conf in query.configs["views"].items():
        template_data = {
            "query": query,
            "view_conf": view_conf
        }
        page_name = "config-view--{view_conf_id}".format(
            view_conf_id=view_conf_id
        )
        _generate_html_page("config_view", template_data, page_name, query.settings)
    
    # Config unwanted pages
    for unwanted_conf_id,unwanted_conf in query.configs["unwanteds"].items():
        template_data = {
            "query": query,
            "unwanted_conf": unwanted_conf
        }
        page_name = "config-unwanted--{unwanted_conf_id}".format(
            unwanted_conf_id=unwanted_conf_id
        )
        _generate_html_page("config_unwanted", template_data, page_name, query.settings)

    log("  Done!")
    log("")

def _generate_repo_pages(query):
    log("Generating repo pages...")

    for repo_id, repo in query.configs["repos"].items():
        for arch in repo["source"]["architectures"]:
            template_data = {
                "query": query,
                "repo": repo,
                "arch": arch
            }
            page_name = "repo--{repo_id}--{arch}".format(
                repo_id=repo_id,
                arch=arch
            )
            _generate_html_page("repo", template_data, page_name, query.settings)


    log("  Done!")
    log("")


def _generate_view_pages(query):
    log("Generating view pages...")

    for view_conf_id,view_conf in query.configs["views"].items():
        if view_conf["type"] == "compose":

            # First, generate the overview page comparing all architectures
            log("  Generating 'compose' view overview {view_conf_id}".format(
                view_conf_id=view_conf_id
            ))

            repo_id = view_conf["repository"]

            # That page needs the number of binary and source packages for each architecture
            arch_pkg_counts = {}
            all_arches_nevrs = set()
            all_arches_unwanteds = set()
            all_arches_source_nvrs = set()
            for arch in query.settings["allowed_arches"]:
                arch_pkg_counts[arch] = {}

                workload_ids = query.workloads_in_view(view_conf_id, arch=arch)

                pkg_ids = query.pkgs_in_view(view_conf_id, arch, output_change="ids")
                pkg_nevrs = query.pkgs_in_view(view_conf_id, arch, output_change="nevrs")
                pkg_binary_names = query.pkgs_in_view(view_conf_id, arch, output_change="binary_names")
                pkg_source_nvr = query.pkgs_in_view(view_conf_id, arch, output_change="source_nvr")
                pkg_source_names = query.pkgs_in_view(view_conf_id, arch, output_change="source_names")
                unwanted_pkgs = query.view_unwanted_pkgs(view_conf_id, arch)

                unwanted_packages_count = 0
                for pkg_name in unwanted_pkgs:
                    if pkg_name in pkg_binary_names:
                        unwanted_packages_count += 1
                        all_arches_unwanteds.add(pkg_name)
                
                arch_pkg_counts[arch]["pkg_ids"] = len(pkg_ids)
                arch_pkg_counts[arch]["pkg_binary_names"] = len(pkg_binary_names)
                arch_pkg_counts[arch]["source_pkg_nvr"] = len(pkg_source_nvr)
                arch_pkg_counts[arch]["source_pkg_names"] = len(pkg_source_names)
                arch_pkg_counts[arch]["unwanted_packages"] = unwanted_packages_count

                all_arches_nevrs.update(pkg_nevrs) 
                all_arches_source_nvrs.update(pkg_source_nvr) 

            template_data = {
                "query": query,
                "view_conf": view_conf,
                "arch_pkg_counts": arch_pkg_counts,
                "all_pkg_count": len(all_arches_nevrs),
                "all_unwanted_count": len(all_arches_unwanteds),
                "all_source_nvr_count": len(all_arches_source_nvrs)
            }
            page_name = "view--{view_conf_id}".format(
                view_conf_id=view_conf_id
            )
            _generate_html_page("view_compose_overview", template_data, page_name, query.settings)

            log("    Done!")
            log("")

            # Second, generate detail pages for each architecture
            for arch in query.arches_in_view(view_conf_id):
                # First, generate the overview page comparing all architectures
                log("  Generating 'compose' view {view_conf_id} for {arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                ))

                template_data = {
                    "query": query,
                    "view_conf": view_conf,
                    "arch": arch,

                }
                page_name = "view--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_html_page("view_compose_packages", template_data, page_name, query.settings)

                page_name = "view-modules--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_html_page("view_compose_modules", template_data, page_name, query.settings)

                page_name = "view-unwanted--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_html_page("view_compose_unwanted", template_data, page_name, query.settings)

                page_name = "view-buildroot--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_html_page("view_compose_buildroot", template_data, page_name, query.settings)

                page_name = "view-workloads--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_html_page("view_compose_workloads", template_data, page_name, query.settings)

                

            # third, generate one page per RPM name

            pkg_names = set()
            buildroot_pkg_names = set()
            all_pkg_names = set()

            #save some useful data for the SRPM pages below
            pkg_name_data = {}

            
            
            all_arches = query.arches_in_view(view_conf_id)

            for arch in all_arches:
                pkg_names.update(query.pkgs_in_view(view_conf_id, arch, output_change="binary_names"))

            buildroot_pkg_srpm_requires = {}
            for arch in all_arches:
                buildroot_pkg_srpm_requires[arch] = query.view_buildroot_pkgs(view_conf_id, arch)

            for arch in all_arches:
                for buildroot_pkg_name in buildroot_pkg_srpm_requires[arch]:
                    buildroot_pkg_names.add(buildroot_pkg_name)
            
            all_pkg_names.update(pkg_names)
            all_pkg_names.update(buildroot_pkg_names)

            for pkg_name in all_pkg_names:

                pkg_ids = {}
                workload_conf_ids_required = {}
                workload_conf_ids_dependency = {}
                workload_conf_ids_env = {}

                required_to_build_srpms = set()

                #pkgs_required_by["this_pkg_id"]["required_by_name"] = set() of required_by_ids
                pkgs_required_by = {}

                exclusion_list_ids = {}
                unwanted_in_view = False

                build_dependency = False

                pkg_srpm_name = None

                # 1: Runtime package stuff
                if pkg_name in pkg_names:

                    for arch in all_arches:

                        for pkg in query.pkgs_in_view(view_conf_id, arch):
                            pkg_nevra = "{name}-{evr}.{arch}".format(
                                name=pkg["name"],
                                evr=pkg["evr"],
                                arch=pkg["arch"]
                            )
                            if pkg["name"] == pkg_name:

                                if pkg_nevra not in pkg_ids:
                                    pkg_ids[pkg_nevra] = set()
                                pkg_ids[pkg_nevra].add(arch)

                                pkg_srpm_name = pkg["source_name"]
                            
                                for workload_id in pkg["q_required_in"]:
                                    workload = query.data["workloads"][workload_id]
                                    workload_conf_id = workload["workload_conf_id"]

                                    if workload_conf_id not in workload_conf_ids_required: 
                                        workload_conf_ids_required[workload_conf_id] = set()
                                    
                                    workload_conf_ids_required[workload_conf_id].add(arch)
                                
                                for workload_id in pkg["q_dep_in"]:
                                    workload = query.data["workloads"][workload_id]
                                    workload_conf_id = workload["workload_conf_id"]

                                    if workload_conf_id not in workload_conf_ids_dependency: 
                                        workload_conf_ids_dependency[workload_conf_id] = set()
                                    
                                    workload_conf_ids_dependency[workload_conf_id].add(arch)
                                
                                for workload_id in pkg["q_env_in"]:
                                    workload = query.data["workloads"][workload_id]
                                    workload_conf_id = workload["workload_conf_id"]

                                    if workload_conf_id not in workload_conf_ids_env: 
                                        workload_conf_ids_env[workload_conf_id] = set()
                                    
                                    workload_conf_ids_env[workload_conf_id].add(arch)

                        for pkg_unwanted_name, pkg_unwanted_data in query.view_unwanted_pkgs(view_conf_id, arch).items():
                            if pkg_name == pkg_unwanted_name:
                                if pkg_unwanted_data["unwanted_in_view"]:
                                    unwanted_in_view = True
                                
                                for exclusion_list_id in pkg_unwanted_data["unwanted_list_ids"]:
                                    if exclusion_list_id not in exclusion_list_ids:
                                        exclusion_list_ids[exclusion_list_id] = set()
                                    
                                    exclusion_list_ids[exclusion_list_id].add(arch)


                    for arch in all_arches:
                        for workload_id in query.workloads_in_view(view_conf_id, arch):
                            workload = query.data["workloads"][workload_id]
                            workload_pkgs = query.workload_pkgs_id(workload_id)
                            workload_pkg_relations = workload["pkg_relations"]

                            for this_pkg_id in pkg_ids:

                                if this_pkg_id not in workload_pkg_relations:
                                    continue

                                if this_pkg_id not in pkgs_required_by:
                                    pkgs_required_by[this_pkg_id] = {}

                                for required_by_id in workload_pkg_relations[this_pkg_id]["required_by"]:
                                    required_by_name = pkg_id_to_name(required_by_id)

                                    if required_by_name not in pkgs_required_by[this_pkg_id]:
                                        pkgs_required_by[this_pkg_id][required_by_name] = set()
                                    
                                    pkgs_required_by[this_pkg_id][required_by_name].add(required_by_id)
                                
                # 2: Buildroot package stuff
                if pkg_name in buildroot_pkg_names:
                    build_dependency = True

                    for buildroot_pkg_relations_conf_id, buildroot_pkg_relations_conf in query.configs["buildroot_pkg_relations"].items():
                        if view_conf_id == buildroot_pkg_relations_conf["view_id"]:
                            arch = buildroot_pkg_relations_conf["arch"]
                            buildroot_pkg_relations = buildroot_pkg_relations_conf["pkg_relations"]

                            for this_pkg_id in buildroot_pkg_relations:
                                this_pkg_name = pkg_id_to_name(this_pkg_id)

                                if this_pkg_name == pkg_name:

                                    if this_pkg_id not in pkg_ids:
                                        pkg_ids[this_pkg_id] = set()
                                    pkg_ids[this_pkg_id].add(arch)

                                    if this_pkg_id in buildroot_pkg_relations and not pkg_srpm_name:
                                        pkg_srpm_name = buildroot_pkg_relations[this_pkg_id]["source_name"]
                            
                            for this_pkg_id in pkg_ids:
                                if this_pkg_id not in buildroot_pkg_relations:
                                    continue

                                if this_pkg_id not in pkgs_required_by:
                                    pkgs_required_by[this_pkg_id] = {}
                                
                                for required_by_id in buildroot_pkg_relations[this_pkg_id]["required_by"]:
                                    required_by_name = pkg_id_to_name(required_by_id)

                                    if required_by_name not in pkgs_required_by[this_pkg_id]:
                                        pkgs_required_by[this_pkg_id][required_by_name] = set()
                                    
                                    pkgs_required_by[this_pkg_id][required_by_name].add(required_by_id + " (buildroot only)")

                    # required to build XX SRPMs
                    for arch in all_arches:
                        if pkg_name in buildroot_pkg_srpm_requires[arch]:
                            required_to_build_srpms.update(set(buildroot_pkg_srpm_requires[arch][pkg_name]["required_by"]))


                template_data = {
                    "query": query,
                    "view_conf": view_conf,
                    "pkg_name": pkg_name,
                    "srpm_name": pkg_srpm_name,
                    "pkg_ids": pkg_ids,
                    "workload_conf_ids_required": workload_conf_ids_required,
                    "workload_conf_ids_dependency": workload_conf_ids_dependency,
                    "workload_conf_ids_env": workload_conf_ids_env,
                    "exclusion_list_ids": exclusion_list_ids,
                    "unwanted_in_view": unwanted_in_view,
                    "pkgs_required_by": pkgs_required_by,
                    "build_dependency": build_dependency,
                    "required_to_build_srpms": required_to_build_srpms
                }
                pkg_name_data[pkg_name] = template_data
                page_name = "view-rpm--{view_conf_id}--{pkg_name}".format(
                    view_conf_id=view_conf_id,
                    pkg_name=pkg_name
                )
                _generate_html_page("view_compose_rpm", template_data, page_name, query.settings)
            
            
            # fourth, generate one page per SRPM name

            srpm_names = set()
            buildroot_srpm_names = set()
            all_srpm_names = set()

            for arch in all_arches:
                srpm_names.update(query.pkgs_in_view(view_conf_id, arch, output_change="source_names"))

            for arch in all_arches:
                buildroot_srpm_names.update(query.view_buildroot_pkgs(view_conf_id, arch, output_change="source_names"))

            srpm_maintainers = query.data["views"][view_conf_id]["srpm_maintainers"]

            all_srpm_names.update(srpm_names)
            all_srpm_names.update(buildroot_srpm_names)

            for srpm_name in all_srpm_names:

                # Since it doesn't include buildroot, yet, I'll need to recreate those manually for now
                if srpm_name in srpm_maintainers:
                    recommended_maintainers = srpm_maintainers[srpm_name]
                else:
                    recommended_maintainers = {}
                    recommended_maintainers["top"] = None
                    recommended_maintainers["all"] = {}

                srpm_pkg_names = set()

                for arch in all_arches:
                    for pkg in query.pkgs_in_view(view_conf_id, arch):
                        if pkg["source_name"] == srpm_name:
                            srpm_pkg_names.add(pkg["name"])
                
                for buildroot_pkg_relations_conf_id, buildroot_pkg_relations_conf in query.configs["buildroot_pkg_relations"].items():
                    if view_conf_id == buildroot_pkg_relations_conf["view_id"]:

                        buildroot_pkg_relations = buildroot_pkg_relations_conf["pkg_relations"]

                        for buildroot_pkg_id, buildroot_pkg in buildroot_pkg_relations.items():
                            if srpm_name == buildroot_pkg["source_name"]:
                                buildroot_pkg_name = pkg_id_to_name(buildroot_pkg_id)
                                srpm_pkg_names.add(buildroot_pkg_name)

                ownership_recommendations = None
                if srpm_name in query.data["views"][view_conf_id]["ownership_recommendations"]:
                    ownership_recommendations = query.data["views"][view_conf_id]["ownership_recommendations"][srpm_name]

                template_data = {
                    "query": query,
                    "view_conf": view_conf,
                    "ownership_recommendations": ownership_recommendations,
                    "recommended_maintainers": recommended_maintainers,
                    "srpm_name": srpm_name,
                    "pkg_names": srpm_pkg_names,
                    "pkg_name_data": pkg_name_data
                }
                page_name = "view-srpm--{view_conf_id}--{srpm_name}".format(
                    view_conf_id=view_conf_id,
                    srpm_name=srpm_name
                )
                _generate_html_page("view_compose_srpm", template_data, page_name, query.settings)



#            # third, generate one page per SRPM
#            all_arches = query.arches_in_view(view_conf_id)
#            all_pkgs = {}
#            for arch in all_arches:
#                all_pkgs[arch] = {}
#                _all_pkgs_list = query.pkgs_in_view(view_conf_id, arch)
#                for pkg in _all_pkgs_list:
#                    all_pkgs[arch][pkg["id"]] = pkg
#            all_workloads = {}
#            for arch in all_arches:
#                all_workload_ids = query.workloads_in_view(view_conf_id, arch)
#                all_workloads[arch] = ""
#                for workload_id in all_workload_ids:
#                    all_workloads[workload_id] = query.data["workloads"][workload_id]
#
#            for srpm_name in pkg_source_names:
#
#                reasons_of_presense = {}
#
#                for arch in all_arches:
#                    for pkg_id, pkg in all_pkgs[arch].items():
#                        # For each package, I need to find all the reasons it's here
#                        # some-other-binary (some-other) pulls this-pkg-binary (this-pkg)
#
#                        if pkg["source_name"] != srpm_name:
#                            continue
#
#                        for workload_id in pkg["q_in"]:
#                            workload = query.data["workloads"][workload_id]
#
#                            for related_pkg_id in workload["pkg_relations"][pkg_id]["required_by"]:
#
#                                related_pkg = all_pkgs[arch][related_pkg_id]
#
#                                if related_pkg_id not in reasons_of_presense:
#                                    reasons_of_presense[related_pkg_id] = {}
#                                    reasons_of_presense[related_pkg_id]["source_name"] = related_pkg["source_name"]
#                                    reasons_of_presense[related_pkg_id]["requires"] = set()
#                                    reasons_of_presense[related_pkg_id]["workload_conf_ids"] = set()
#                                
#                                reasons_of_presense[related_pkg_id]["requires"].add(pkg_id)
#                                reasons_of_presense[related_pkg_id]["workload_conf_ids"].add(workload["workload_conf_id"])
#
#
#                pkgs = {}
#                srpm_arches = set()
#
#                for arch, arch_pkgs in all_pkgs.items():
#                    for pkg_id, pkg in all_pkgs[arch].items():
#                        if pkg["source_name"] == srpm_name:
#                            pkg_nevr = "{name}-{evr}".format(
#                                name=pkg["name"],
#                                evr=pkg["evr"]
#                            )
#
#                            if pkg_nevr not in pkgs:
#                                pkgs[pkg_nevr] = {}
#                            
#                            if arch not in pkgs[pkg_nevr]:
#                                pkgs[pkg_nevr][arch] = {}
#                            
#                            pkgs[pkg_nevr][arch][pkg["id"]] = pkg
#                            srpm_arches.add(arch)
#
#                template_data = {
#                    "query": query,
#                    "view_conf": view_conf,
#                    "srpm_name": srpm_name,
#                    "pkgs": pkgs,
#                    "arches": sorted(list(srpm_arches)),
#                    "reasons_of_presense": reasons_of_presense
#                }
#                page_name = "view-srpm--{view_conf_id}--{srpm_name}".format(
#                    view_conf_id=view_conf_id,
#                    srpm_name=srpm_name
#                )
#                _generate_html_page("view_compose_srpm_package", template_data, page_name, query.settings)
#

    log("  Done!")
    log("")


def _generate_a_flat_list_file(data_list, file_name, settings):

    file_contents = "\n".join(data_list)

    filename = ("{file_name}.txt".format(
        file_name=file_name.replace(":", "--")
    ))

    output = settings["output"]

    log("  Writing file...  ({filename})".format(
        filename=filename
    ))
    with open(os.path.join(output, filename), "w") as file:
        file.write(file_contents)


def _generate_view_lists(query):
    log("Generating view lists...")

    for view_conf_id,view_conf in query.configs["views"].items():
        if view_conf["type"] == "compose":

            repo_id = view_conf["repository"]

            for arch in query.arches_in_view(view_conf_id):
                # First, generate the overview page comparing all architectures
                log("  Generating 'compose' package list {view_conf_id} for {arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                ))


                pkg_ids = query.pkgs_in_view(view_conf_id, arch, output_change="ids")
                pkg_binary_names = query.pkgs_in_view(view_conf_id, arch, output_change="binary_names")
                pkg_source_nvr = query.pkgs_in_view(view_conf_id, arch, output_change="source_nvr")
                pkg_source_names = query.pkgs_in_view(view_conf_id, arch, output_change="source_names")
                
                buildroot_data = query.view_buildroot_pkgs(view_conf_id, arch)
                pkg_buildroot_source_names = query.view_buildroot_pkgs(view_conf_id, arch, output_change="source_names")
                if buildroot_data:
                    pkg_buildroot_names = buildroot_data.keys()
                else:
                    pkg_buildroot_names = []
                
                modules = query.view_modules(view_conf_id, arch)

                file_name = "view-binary-package-list--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_a_flat_list_file(pkg_ids, file_name, query.settings)

                file_name = "view-binary-package-name-list--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_a_flat_list_file(pkg_binary_names, file_name, query.settings)

                file_name = "view-source-package-list--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_a_flat_list_file(pkg_source_nvr, file_name, query.settings)
    
                file_name = "view-source-package-name-list--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_a_flat_list_file(pkg_source_names, file_name, query.settings)

                file_name = "view-buildroot-package-name-list--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_a_flat_list_file(pkg_buildroot_names, file_name, query.settings)

                file_name = "view-buildroot-source-package-name-list--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_a_flat_list_file(pkg_buildroot_source_names, file_name, query.settings)

                file_name = "view-module-list--{view_conf_id}--{arch}".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                _generate_a_flat_list_file(modules, file_name, query.settings)

                file_name = "view-placeholder-srpm-details--{view_conf_id}--{arch}.json".format(
                    view_conf_id=view_conf_id,
                    arch=arch
                )
                file_path = os.path.join(query.settings["output"], file_name)
                view_placeholder_srpm_details = query.view_placeholder_srpms(view_conf_id, arch)
                dump_data(file_path, view_placeholder_srpm_details)

    
    log("  Done!")
    log("")


def _dump_all_data(query):
    log("Dumping all data...")

    data = {}
    data["data"] = query.data
    data["configs"] = query.configs
    data["settings"] = query.settings

    file_name = "data.json"
    file_path = os.path.join(query.settings["output"], file_name)
    dump_data(file_path, data)

    log("  Done!")
    log("")


def generate_pages(query):
    log("")
    log("###############################################################################")
    log("### Generating html pages! ####################################################")
    log("###############################################################################")
    log("")

    # Copy static files
    log("Copying static files...")
    src_static_dir = os.path.join("templates", "_static")
    output_static_dir = os.path.join(query.settings["output"])
    subprocess.run(["cp", "-R", src_static_dir, output_static_dir])
    log("  Done!")
    log("")

    # Generate the landing page
    _generate_html_page("homepage", None, "index", query.settings)

    # Generate the main menu page
    _generate_html_page("results", None, "results", query.settings)

    # Generate config pages
    _generate_config_pages(query)

    # Generate the top-level results pages
    template_data = {
        "query": query
    }
    _generate_html_page("repos", template_data, "repos", query.settings)
    _generate_html_page("envs", template_data, "envs", query.settings)
    _generate_html_page("workloads", template_data, "workloads", query.settings)
    _generate_html_page("labels", template_data, "labels", query.settings)
    _generate_html_page("views", template_data, "views", query.settings)
    _generate_html_page("maintainers", template_data, "maintainers", query.settings)
    
    # Generate repo pages
    _generate_repo_pages(query)

    # Generate maintainer pages
    _generate_maintainer_pages(query)

    # Generate env_overview pages
    _generate_env_pages(query)

    # Generate workload_overview pages
    _generate_workload_pages(query)

    # Generate view pages
    _generate_view_pages(query)

    # Generate flat lists for views
    _generate_view_lists(query)

    # Dump all data
    _dump_all_data(query)

    # Generate the errors page
    template_data = {
        "query": query
    }
    _generate_html_page("errors", template_data, "errors", query.settings)



    log("")
    log("###############################################################################")
    log("### Generating JSON pages! ####################################################")
    log("###############################################################################")
    log("")

    # Generate data for the top-level results pages
    maintainer_data = query.maintainers()
    _generate_json_page(maintainer_data, "maintainers", query.settings)





###############################################################################
### Historic Data #############################################################
###############################################################################

# This is generating historic (and present) package lists
# Data for the historic charts is the function below
def _save_package_history(query):
    log("Generating current package history lists...")


    # /history/
    # /history/2020-week_28/
    # /history/2020-week_28/workload--WORKLOAD_ID.json
    # /history/2020-week_28/workload-conf--WORKLOAD_CONF_ID.json
    # /history/2020-week_28/env--ENV_ID.json
    # /history/2020-week_28/env-conf--ENV_CONF_ID.json
    # /history/2020-week_28/view--VIEW_CONF_ID.json

    # Where to save it
    year = datetime.datetime.now().strftime("%Y")
    week = datetime.datetime.now().strftime("%W")
    date = str(datetime.datetime.now().strftime("%Y-%m-%d"))

    output_dir = os.path.join(query.settings["output"], "history")
    output_subdir = "{year}-week_{week}".format(
        year=year,
        week=week
    )
    subprocess.run(["mkdir", "-p", os.path.join(output_dir, output_subdir)])

    # Also save the current data to the standard output dir
    current_version_output_dir = query.settings["output"]

    # == Workloads
    log("")
    log("Workloads:")
    for workload_conf_id, workload_conf in query.configs["workloads"].items():

        # === Config

        log("")
        log("  Config for: {}".format(workload_conf_id))

        # Where to save
        filename = "workload-conf--{workload_conf_id_slug}.json".format(
            workload_conf_id_slug = query.url_slug_id(workload_conf_id)
        )
        file_path = os.path.join(output_dir, output_subdir, filename)
        current_version_file_path = os.path.join(current_version_output_dir, filename)

        # What to save
        output_data = {}
        output_data["date"] = date
        output_data["id"] = workload_conf_id
        output_data["type"] = "workload_conf"
        output_data["data"] = query.configs["workloads"][workload_conf_id]

        # And save it
        log("    Saving in: {file_path}".format(
            file_path=file_path
        ))
        dump_data(file_path, output_data)

        # Also save the current data to the standard output dir
        log("    Saving in: {current_version_file_path}".format(
            current_version_file_path=current_version_file_path
        ))
        dump_data(current_version_file_path, output_data)


        # === Results

        for workload_id in query.workloads(workload_conf_id, None, None, None, list_all=True):
            workload = query.data["workloads"][workload_id]

            log("  Results: {}".format(workload_id))

            # Where to save
            filename = "workload--{workload_id_slug}.json".format(
                workload_id_slug = query.url_slug_id(workload_id)
            )
            file_path = os.path.join(output_dir, output_subdir, filename)
            current_version_file_path = os.path.join(current_version_output_dir, filename)

            # What to save
            output_data = {}
            output_data["date"] = date
            output_data["id"] = workload_id
            output_data["type"] = "workload"
            output_data["data"] = query.data["workloads"][workload_id]
            output_data["pkg_query"] = query.workload_pkgs_id(workload_id)

            # And save it
            log("    Saving in: {file_path}".format(
                file_path=file_path
            ))
            dump_data(file_path, output_data)

            # Also save the current data to the standard output dir
            log("    Saving in: {current_version_file_path}".format(
                current_version_file_path=current_version_file_path
            ))
            dump_data(current_version_file_path, output_data)
    
    # == envs
    log("")
    log("Envs:")
    for env_conf_id, env_conf in query.configs["envs"].items():

        # === Config

        log("")
        log("  Config for: {}".format(env_conf_id))

        # Where to save
        filename = "env-conf--{env_conf_id_slug}.json".format(
            env_conf_id_slug = query.url_slug_id(env_conf_id)
        )
        file_path = os.path.join(output_dir, output_subdir, filename)
        current_version_file_path = os.path.join(current_version_output_dir, filename)

        # What to save
        output_data = {}
        output_data["date"] = date
        output_data["id"] = env_conf_id
        output_data["type"] = "env_conf"
        output_data["data"] = query.configs["envs"][env_conf_id]

        # And save it
        log("    Saving in: {file_path}".format(
            file_path=file_path
        ))
        dump_data(file_path, output_data)

        # Also save the current data to the standard output dir
        log("    Saving in: {current_version_file_path}".format(
            current_version_file_path=current_version_file_path
        ))
        dump_data(current_version_file_path, output_data)


        # === Results

        for env_id in query.envs(env_conf_id, None, None, list_all=True):
            env = query.data["envs"][env_id]

            log("  Results: {}".format(env_id))

            # Where to save
            filename = "env--{env_id_slug}.json".format(
                env_id_slug = query.url_slug_id(env_id)
            )
            file_path = os.path.join(output_dir, output_subdir, filename)
            current_version_file_path = os.path.join(current_version_output_dir, filename)

            # What to save
            output_data = {}
            output_data["date"] = date
            output_data["id"] = env_id
            output_data["type"] = "env"
            output_data["data"] = query.data["envs"][env_id]
            output_data["pkg_query"] = query.env_pkgs_id(env_id)

            # And save it
            log("    Saving in: {file_path}".format(
                file_path=file_path
            ))
            dump_data(file_path, output_data)

            # Also save the current data to the standard output dir
            log("    Saving in: {current_version_file_path}".format(
                current_version_file_path=current_version_file_path
            ))
            dump_data(current_version_file_path, output_data)
    
    # == views
    log("")
    log("views:")
    for view_conf_id, view_conf in query.configs["views"].items():

        # === Config

        log("")
        log("  Config for: {}".format(view_conf_id))

        # Where to save
        filename = "view-conf--{view_conf_id_slug}.json".format(
            view_conf_id_slug = query.url_slug_id(view_conf_id)
        )
        file_path = os.path.join(output_dir, output_subdir, filename)
        current_version_file_path = os.path.join(current_version_output_dir, filename)

        # What to save
        output_data = {}
        output_data["date"] = date
        output_data["id"] = view_conf_id
        output_data["type"] = "view_conf"
        output_data["data"] = query.configs["views"][view_conf_id]

        # And save it
        log("    Saving in: {file_path}".format(
            file_path=file_path
        ))
        dump_data(file_path, output_data)

        # Also save the current data to the standard output dir
        log("    Saving in: {current_version_file_path}".format(
            current_version_file_path=current_version_file_path
        ))
        dump_data(current_version_file_path, output_data)


        # === Results

        for arch in query.arches_in_view(view_conf_id):

            log("  Results: {}".format(env_id))

            view_id = "{view_conf_id}:{arch}".format(
                view_conf_id=view_conf_id,
                arch=arch
            )

            # Where to save
            filename = "view--{view_id_slug}.json".format(
                view_id_slug = query.url_slug_id(view_id)
            )
            file_path = os.path.join(output_dir, output_subdir, filename)
            current_version_file_path = os.path.join(current_version_output_dir, filename)

            # What to save
            output_data = {}
            output_data["date"] = date
            output_data["id"] = view_id
            output_data["type"] = "view"
            output_data["workload_ids"] = query.workloads_in_view(view_conf_id, arch)
            output_data["pkg_query"] = query.pkgs_in_view(view_conf_id, arch)
            output_data["unwanted_pkg"] = query.view_unwanted_pkgs(view_conf_id, arch)
            

            # And save it
            log("    Saving in: {file_path}".format(
                file_path=file_path
            ))
            dump_data(file_path, output_data)

            # Also save the current data to the standard output dir
            log("    Saving in: {current_version_file_path}".format(
                current_version_file_path=current_version_file_path
            ))
            dump_data(current_version_file_path, output_data)


            # == Also, save the buildroot data

            # Where to save
            filename = "view-buildroot--{view_id_slug}.json".format(
                view_id_slug = query.url_slug_id(view_id)
            )
            file_path = os.path.join(output_dir, output_subdir, filename)
            current_version_file_path = os.path.join(current_version_output_dir, filename)

            # What to save
            output_data = {}
            output_data["date"] = date
            output_data["id"] = view_id
            output_data["type"] = "view-buildroot"
            output_data["pkgs"] = query.view_buildroot_pkgs(view_conf_id, arch)

            # And save it
            log("    Saving in: {file_path}".format(
                file_path=file_path
            ))
            dump_data(file_path, output_data)

            # Also save the current data to the standard output dir
            log("    Saving in: {current_version_file_path}".format(
                current_version_file_path=current_version_file_path
            ))
            dump_data(current_version_file_path, output_data)


    log("  Done!")
    log("")


# This is the historic data for charts
# Package lists are above 
def _save_current_historic_data(query):
    log("Generating current historic data...")

    # Where to save it
    year = datetime.datetime.now().strftime("%Y")
    week = datetime.datetime.now().strftime("%W")
    filename = "historic_data-{year}-week_{week}.json".format(
        year=year,
        week=week
    )
    output_dir = os.path.join(query.settings["output"], "history")
    file_path = os.path.join(output_dir, filename)

    # What to save there
    history_data = {}
    history_data["date"] = str(datetime.datetime.now().strftime("%Y-%m-%d"))
    history_data["workloads"] = {}
    history_data["envs"] = {}
    history_data["repos"] = {}
    history_data["views"] = {}

    for workload_id in query.workloads(None,None,None,None,list_all=True):
        workload = query.data["workloads"][workload_id]

        if not workload["succeeded"]:
            continue

        workload_history = {}
        workload_history["size"] = query.workload_size_id(workload_id)
        workload_history["pkg_count"] = len(query.workload_pkgs_id(workload_id))

        history_data["workloads"][workload_id] = workload_history
    
    for env_id in query.envs(None,None,None,list_all=True):
        env = query.data["envs"][env_id]

        if not env["succeeded"]:
            continue

        env_history = {}
        env_history["size"] = query.env_size_id(env_id)
        env_history["pkg_count"] = len(query.env_pkgs_id(env_id))

        history_data["envs"][env_id] = env_history

    for repo_id in query.configs["repos"].keys():
        history_data["repos"][repo_id] = {}

        for arch, pkgs in query.data["pkgs"][repo_id].items():

            repo_history = {}
            repo_history["pkg_count"] = len(pkgs)
            
            history_data["repos"][repo_id][arch] = repo_history
    
    for view_conf_id in query.configs["views"].keys():
        history_data["views"][view_conf_id] = {}

        for arch in query.arches_in_view(view_conf_id):

            pkg_ids = query.pkgs_in_view(view_conf_id, arch)

            view_history = {}
            view_history["pkg_count"] = len(pkg_ids)
            
            history_data["views"][view_conf_id][arch] = view_history

    # And save it
    log("  Saving in: {file_path}".format(
        file_path=file_path
    ))
    dump_data(file_path, history_data)

    log("  Done!")
    log("")


def _read_historic_data(query):
    log("Reading historic data...")

    directory = os.path.join(query.settings["output"], "history")

    # Do some basic validation of the filename
    all_filenames = os.listdir(directory)
    valid_filenames = []
    for filename in all_filenames:
        if bool(re.match("historic_data-....-week_...json", filename)):
            valid_filenames.append(filename)
    valid_filenames.sort()

    # Get the data
    historic_data = {}

    for filename in valid_filenames:
        with open(os.path.join(directory, filename), "r") as file:
            try:
                document = json.load(file)

                date = datetime.datetime.strptime(document["date"],"%Y-%m-%d")
                year = date.strftime("%Y")
                week = date.strftime("%W")
                key = "{year}-week_{week}".format(
                    year=year,
                    week=week
                )
            except (KeyError, ValueError):
                err_log("Invalid file in historic data: {filename}. Ignoring.".format(
                    filename=filename
                ))
                continue

            historic_data[key] = document

    return historic_data

    log("  Done!")
    log("")


def _save_json_data_entry(entry_name, entry_data, settings):
    log("Generating data entry for {entry_name}".format(
        entry_name=entry_name
    ))

    output = settings["output"]

    filename = ("{entry_name}.json".format(
        entry_name=entry_name.replace(":", "--")
    ))

    log("  Writing file...  ({filename})".format(
        filename=filename
    ))

    with open(os.path.join(output, filename), "w") as file:
        json.dump(entry_data, file)
    
    log("  Done!")
    log("")


def _generate_chartjs_data(historic_data, query):

    # Data for workload pages
    for workload_id in query.workloads(None, None, None, None, list_all=True):

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []
        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        workload = query.data["workloads"][workload_id]
        workload_conf_id = workload["workload_conf_id"]
        workload_conf = query.configs["workloads"][workload_conf_id]

        dataset = {}
        dataset["data"] = []
        dataset["label"] = workload_conf["name"]
        dataset["fill"] = "false"

        for _,entry in historic_data.items():
            try:
                size = entry["workloads"][workload_id]["size"]

                # The chart needs the size in MB, but just as a number
                size_mb = "{0:.1f}".format(size/1024/1024)
                dataset["data"].append(size_mb)
            except KeyError:
                dataset["data"].append("null")

        entry_data["datasets"].append(dataset)

        entry_name = "chartjs-data--workload--{workload_id}".format(
            workload_id=workload_id
        )
        _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for workload overview pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for workload_id in query.workloads(workload_conf_id, None, repo_id, None, list_all=True):

                workload = query.data["workloads"][workload_id]
                env_conf_id = workload["env_conf_id"]
                env_conf = query.configs["envs"][env_conf_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "in {name} {arch}".format(
                    name=env_conf["name"],
                    arch=workload["arch"]
                )
                dataset["fill"] = "false"


                for _,entry in historic_data.items():
                    try:
                        size = entry["workloads"][workload_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--workload-overview--{workload_conf_id}--{repo_id}".format(
                workload_conf_id=workload_conf_id,
                repo_id=repo_id
            )
            _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for workload cmp arches pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for env_conf_id in query.workloads(workload_conf_id,None,None,None,output_change="env_conf_ids"):
            for repo_id in query.workloads(workload_conf_id,env_conf_id,None,None,output_change="repo_ids"):

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                entry_data = {}

                # First, get the dates as chart labels
                entry_data["labels"] = []
                for _,entry in historic_data.items():
                    date = entry["date"]
                    entry_data["labels"].append(date)

                # Second, get the actual data for everything that's needed
                entry_data["datasets"] = []

                for workload_id in query.workloads(workload_conf_id,env_conf_id,repo_id,None,list_all=True):

                    workload = query.data["workloads"][workload_id]
                    env_conf_id = workload["env_conf_id"]
                    env_conf = query.configs["envs"][env_conf_id]

                    dataset = {}
                    dataset["data"] = []
                    dataset["label"] = "{arch}".format(
                        arch=workload["arch"]
                    )
                    dataset["fill"] = "false"

                    for _,entry in historic_data.items():
                        try:
                            size = entry["workloads"][workload_id]["size"]

                            # The chart needs the size in MB, but just as a number
                            size_mb = "{0:.1f}".format(size/1024/1024)
                            dataset["data"].append(size_mb)
                        except KeyError:
                            dataset["data"].append("null")

                    entry_data["datasets"].append(dataset)

                entry_name = "chartjs-data--workload-cmp-arches--{workload_conf_id}--{env_conf_id}--{repo_id}".format(
                    workload_conf_id=workload_conf_id,
                    env_conf_id=env_conf_id,
                    repo_id=repo_id
                )
                _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for workload cmp envs pages
    for workload_conf_id in query.workloads(None,None,None,None,output_change="workload_conf_ids"):
        for repo_id in query.workloads(workload_conf_id,None,None,None,output_change="repo_ids"):
            for arch in query.workloads(workload_conf_id,None,repo_id,None,output_change="arches"):

                workload_conf = query.configs["workloads"][workload_conf_id]
                env_conf = query.configs["envs"][env_conf_id]
                repo = query.configs["repos"][repo_id]

                entry_data = {}

                # First, get the dates as chart labels
                entry_data["labels"] = []
                for _,entry in historic_data.items():
                    date = entry["date"]
                    entry_data["labels"].append(date)

                # Second, get the actual data for everything that's needed
                entry_data["datasets"] = []

                for workload_id in query.workloads(workload_conf_id,None,repo_id,arch,list_all=True):

                    workload = query.data["workloads"][workload_id]
                    repo = query.configs["repos"][repo_id]

                    dataset = {}
                    dataset["data"] = []
                    dataset["label"] = "{repo} {arch}".format(
                        repo=repo["name"],
                        arch=workload["arch"]
                    )
                    dataset["fill"] = "false"

                    for _,entry in historic_data.items():
                        try:
                            size = entry["workloads"][workload_id]["size"]

                            # The chart needs the size in MB, but just as a number
                            size_mb = "{0:.1f}".format(size/1024/1024)
                            dataset["data"].append(size_mb)
                        except KeyError:
                            dataset["data"].append("null")

                    entry_data["datasets"].append(dataset)

                entry_name = "chartjs-data--workload-cmp-envs--{workload_conf_id}--{repo_id}--{arch}".format(
                    workload_conf_id=workload_conf_id,
                    repo_id=repo_id,
                    arch=arch
                )
                _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for env pages
    for env_id in query.envs(None, None, None, list_all=True):

        entry_data = {}

        # First, get the dates as chart labels
        entry_data["labels"] = []
        for _,entry in historic_data.items():
            date = entry["date"]
            entry_data["labels"].append(date)

        # Second, get the actual data for everything that's needed
        entry_data["datasets"] = []

        env = query.data["envs"][env_id]
        env_conf_id = env["env_conf_id"]
        env_conf = query.configs["envs"][env_conf_id]

        dataset = {}
        dataset["data"] = []
        dataset["label"] = env_conf["name"]
        dataset["fill"] = "false"


        for _,entry in historic_data.items():
            try:
                size = entry["envs"][env_id]["size"]

                # The chart needs the size in MB, but just as a number
                size_mb = "{0:.1f}".format(size/1024/1024)
                dataset["data"].append(size_mb)
            except KeyError:
                dataset["data"].append("null")

        entry_data["datasets"].append(dataset)

        entry_name = "chartjs-data--env--{env_id}".format(
            env_id=env_id
        )
        _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for env overview pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for env_id in query.envs(env_conf_id, repo_id, None, list_all=True):

                env = query.data["envs"][env_id]
                env_conf_id = env["env_conf_id"]
                env_conf = query.configs["envs"][env_conf_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "in {name} {arch}".format(
                    name=env_conf["name"],
                    arch=env["arch"]
                )
                dataset["fill"] = "false"


                for _,entry in historic_data.items():
                    try:
                        size = entry["envs"][env_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--env-overview--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for env cmp arches pages
    for env_conf_id in query.envs(None,None,None,output_change="env_conf_ids"):
        for repo_id in query.envs(env_conf_id,None,None,output_change="repo_ids"):

            env_conf = query.configs["envs"][env_conf_id]
            env_conf = query.configs["envs"][env_conf_id]
            repo = query.configs["repos"][repo_id]

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            for env_id in query.envs(env_conf_id,repo_id,None,list_all=True):

                env = query.data["envs"][env_id]

                dataset = {}
                dataset["data"] = []
                dataset["label"] = "{arch}".format(
                    arch=env["arch"]
                )
                dataset["fill"] = "false"

                for _,entry in historic_data.items():
                    try:
                        size = entry["envs"][env_id]["size"]

                        # The chart needs the size in MB, but just as a number
                        size_mb = "{0:.1f}".format(size/1024/1024)
                        dataset["data"].append(size_mb)
                    except KeyError:
                        dataset["data"].append("null")

                entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--env-cmp-arches--{env_conf_id}--{repo_id}".format(
                env_conf_id=env_conf_id,
                repo_id=repo_id
            )
            _save_json_data_entry(entry_name, entry_data, query.settings)
    
    # Data for compose view pages    
    for view_conf_id in query.configs["views"].keys():

        for arch in query.arches_in_view(view_conf_id):

            entry_data = {}

            # First, get the dates as chart labels
            entry_data["labels"] = []
            for _,entry in historic_data.items():
                date = entry["date"]
                entry_data["labels"].append(date)

            # Second, get the actual data for everything that's needed
            entry_data["datasets"] = []

            dataset = {}
            dataset["data"] = []
            dataset["label"] = "Number of packages"
            dataset["fill"] = "false"

            for _,entry in historic_data.items():
                try:
                    count = entry["views"][view_conf_id][arch]["pkg_count"]
                    dataset["data"].append(count)
                except KeyError:
                    dataset["data"].append("null")

            entry_data["datasets"].append(dataset)

            entry_name = "chartjs-data--view--{view_conf_id}--{arch}".format(
                view_conf_id=view_conf_id,
                arch=arch
            )
            _save_json_data_entry(entry_name, entry_data, query.settings)


def generate_historic_data(query):
    log("")
    log("###############################################################################")
    log("### Historic Data #############################################################")
    log("###############################################################################")
    log("")

    # Save historic package lists
    _save_package_history(query)

    # Step 1: Save current data
    _save_current_historic_data(query)

    # Step 2: Read historic data
    historic_data = _read_historic_data(query)

    # Step 3: Generate Chart.js data
    _generate_chartjs_data(historic_data, query)


class OwnershipEngine:
    # Levels:
    #
    #  
    # level0 == required
    # ---
    # level1 == 1st level runtime dep
    # ...
    # level9 == 9th level runtime dep
    #
    #  
    # level10 == build dep of something in the previous group
    # --- 
    # level11 == 1st level runtime dep 
    # ...
    # level19 == 9th level runtime dep
    #
    #  
    # level20 == build dep of something in the previous group
    # level21 == 1st level runtime dep 
    # ...
    # level29 == 9th level runtime dep
    #
    # etc. up to level99


    def __init__(self, query):
        self.query = query
        self.MAX_LEVEL = 9
        self.MAX_LAYER = 9
        self.skipped_maintainers = ["bakery", "jwboyer", "asamalik"]

    
    def process_view(self, view_conf_id):
        self._initiate_view(view_conf_id)

        log("Processing ownership recommendations for {} view...".format(view_conf_id))

        # Layer 0
        log("  Processing Layer 0...")
        self._process_layer_zero_entries()
        self._process_layer_component_maintainers()


        # Layers 1-9
        # This operates on all SRPMs from the previous level.
        # Resolves all their build dependencies. 
        previous_layer_srpms = self.runtime_srpm_names
        for layer in range(1, self.MAX_LAYER + 1):
            log("  Processing Layer {}...".format(layer))
            log("    {} components".format(len(previous_layer_srpms)))
            # Process all the "pkg_entries" for this layer, and collect this layer srpm packages
            # which will be used in the next layer.
            this_layer_srpm_packages = self._process_layer_pkg_entries(layer, previous_layer_srpms)
            self._process_layer_srpm_entries(layer)
            self._process_layer_component_maintainers()
            previous_layer_srpms = this_layer_srpm_packages
        
        log("Done!")
        log("")

        return self.component_maintainers


    def _process_layer_pkg_entries(self, layer, build_srpm_names):

        if layer not in range(1,10):
            raise ValueError

        level_srpm_packages = set()

        for build_srpm_name in build_srpm_names:

            # Packages on level 0 == required
            level = 0
            level_name = "level{}{}".format(layer, level)
            level0_pkg_names = set()


            # This will initially hold all packages.
            # When figuring out levels, I'll process each package just once.
            # And for that I'll be removing them from this set as I go.
            remaining_pkg_names = self.buildroot_only_rpm_names.copy()

            #for pkg_name, pkg in self.buildroot_pkgs.items():
            for pkg_name in remaining_pkg_names.copy():
                pkg = self.buildroot_pkgs[pkg_name]
                if build_srpm_name in pkg["required_by_srpms"]:

                    if "source_name" not in self.pkg_entries[pkg_name]:
                        self.pkg_entries[pkg_name]["source_name"] = pkg["source_name"]

                    self.pkg_entries[pkg_name][level_name]["build_source_names"].add(build_srpm_name)
                    level0_pkg_names.add(pkg_name)
                    remaining_pkg_names.discard(pkg_name)
                    level_srpm_packages.add(pkg["source_name"])


            pkg_names_level = []
            pkg_names_level.append(level0_pkg_names)

            # Starting at level 1, because level 0 is already done (that's required packages)
            for level in range(1, self.MAX_LEVEL + 1):
                level_name = "level{}{}".format(layer, level)

                #1..
                pkg_names_level.append(set())

                #for pkg_name, pkg in self.buildroot_pkgs.items():
                for pkg_name in remaining_pkg_names.copy():
                    pkg = self.buildroot_pkgs[pkg_name]
                    for higher_pkg_name in pkg["required_by"]:
                        if higher_pkg_name in pkg_names_level[level - 1]:

                            if "source_name" not in self.pkg_entries[pkg_name]:
                                self.pkg_entries[pkg_name]["source_name"] = pkg["source_name"]
                            
                            self.pkg_entries[pkg_name][level_name]["build_source_names"].add(build_srpm_name)
                            pkg_names_level[level].add(pkg_name)
                            remaining_pkg_names.discard(pkg_name)
                            level_srpm_packages.add(pkg["source_name"])
        
        return level_srpm_packages


    def _process_layer_srpm_entries(self, layer):

        if layer not in range(1,10):
            raise ValueError

        for pkg_name, pkg in self.pkg_entries.items():
            if "source_name" not in pkg:
                continue

            source_name = pkg["source_name"]

            for level in range(0, self.MAX_LEVEL + 1):
                level_name = "level{}{}".format(layer, level)

                for build_srpm_name in pkg[level_name]["build_source_names"]:
                    top_maintainers = self.component_maintainers[build_srpm_name]["top_multiple"]

                    for maintainer in top_maintainers:

                        if maintainer not in self.srpm_entries[source_name]["ownership"][level_name]:
                            self.srpm_entries[source_name]["ownership"][level_name][maintainer] = {}
                            self.srpm_entries[source_name]["ownership"][level_name][maintainer]["build_source_names"] = {}
                            self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_count"] = 0
                        
                        if build_srpm_name not in self.srpm_entries[source_name]["ownership"][level_name][maintainer]["build_source_names"]:
                            self.srpm_entries[source_name]["ownership"][level_name][maintainer]["build_source_names"][build_srpm_name] = set()

                        self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_count"] += 1
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer]["build_source_names"][build_srpm_name].add(pkg_name)


    def _process_layer_component_maintainers(self):

        clear_components = set()
        unclear_components = set()

        for component_name, owner_data in self.srpm_entries.items():

            if self.component_maintainers[component_name]["top"]:
                continue

            found = False
            maintainers = {}
            top_maintainer = None
            top_maintainers = set()

            for level_name, level_data in owner_data["ownership"].items():
                if found:
                    break

                if not level_data:
                    continue

                for maintainer, maintainer_data in level_data.items():

                    if maintainer in self.skipped_maintainers:
                        continue

                    found = True
                    
                    maintainers[maintainer] = maintainer_data["pkg_count"]
            
            # Sort out maintainers based on their score
            maintainer_scores = {}
            for maintainer, score in maintainers.items():
                if score not in maintainer_scores:
                    maintainer_scores[score] = set()
                maintainer_scores[score].add(maintainer)

            # Going through the scores, starting with the highest
            for score in sorted(maintainer_scores, reverse=True):
                # If there are multiple with the same score, it's unclear
                if len(maintainer_scores[score]) > 1:
                    for chosen_maintainer in maintainer_scores[score]:
                        top_maintainers.add(chosen_maintainer)
                    break

                # If there's just one maintainer with this score, it's the owner!
                if len(maintainer_scores[score]) == 1:
                    for chosen_maintainer in maintainer_scores[score]:
                        top_maintainer = chosen_maintainer
                        top_maintainers.add(chosen_maintainer)
                    break
                    
            self.component_maintainers[component_name]["all"] = maintainers
            self.component_maintainers[component_name]["top_multiple"] = top_maintainers
            self.component_maintainers[component_name]["top"] = top_maintainer



    
    def _initiate_view(self, view_conf_id):
        self.view_conf_id = view_conf_id
        self.all_arches = self.query.arches_in_view(view_conf_id)

        self.workload_ids = self.query.workloads_in_view(view_conf_id, None)

        self.pkg_entries = {}
        self.srpm_entries = {}
        self.component_maintainers = {}

        self.runtime_rpm_names = set()
        self.runtime_srpm_names = set()

        self.buildroot_rpm_names = set()
        self.buildroot_srpm_names = set()

        self.buildroot_only_rpm_names = set()
        self.buildroot_only_srpm_names = set()

        self.all_rpm_names = set()
        self.all_srpm_names = set()

        self.buildroot_pkgs = {}
        # {
        #   "RPM_NAME": {
        #       "source_name": "SRPM_NAME",
        #       "required_by": set(
        #           "RPM_NAME", 
        #           "RPM_NAME", 
        #       ),
        #       "required_by_srpms": set(
        #           "SRPM_NAME",
        #           "SRPM_NAME",
        #       ),
        #   } 
        # }


        ### Initiate: self.runtime_rpm_names
        for arch in self.all_arches:
            self.runtime_rpm_names.update(self.query.pkgs_in_view(view_conf_id, arch, output_change="binary_names"))


        ### Initiate: self.runtime_srpm_names
        for arch in self.all_arches:
            self.runtime_srpm_names.update(self.query.pkgs_in_view(view_conf_id, arch, output_change="source_names"))
        

        ### Initiate: self.buildroot_pkgs
        build_dependencies = {}
        for arch in self.all_arches:
            for pkg_name, pkg_data in self.query.view_buildroot_pkgs(view_conf_id, arch).items():
                if pkg_name not in build_dependencies:
                    build_dependencies[pkg_name] = {}
                    build_dependencies[pkg_name]["required_by"] = set()
                build_dependencies[pkg_name]["required_by"] = build_dependencies[pkg_name]["required_by"].union(pkg_data["required_by"])
        
        buildroot_pkg_relations = {}
        for buildroot_pkg_relations_conf_id, buildroot_pkg_relations_conf in self.query.configs["buildroot_pkg_relations"].items():
            if view_conf_id == buildroot_pkg_relations_conf["view_id"]:
                arch = buildroot_pkg_relations_conf["arch"]
                arch_buildroot_pkg_relations = buildroot_pkg_relations_conf["pkg_relations"]

                for pkg_id, pkg_data in arch_buildroot_pkg_relations.items():
                    pkg_name = pkg_id_to_name(pkg_id)
                    if pkg_name not in buildroot_pkg_relations:
                        buildroot_pkg_relations[pkg_name] = {}
                        buildroot_pkg_relations[pkg_name]["source_name"] = pkg_data["source_name"]
                        buildroot_pkg_relations[pkg_name]["required_by"] = set()
                    for required_by_pkg_id in pkg_data["required_by"]:
                        required_by_pkg_name = pkg_id_to_name(required_by_pkg_id)
                        buildroot_pkg_relations[pkg_name]["required_by"].add(required_by_pkg_name)

        for pkg_name, pkg in buildroot_pkg_relations.items():
            if pkg_name not in build_dependencies:
                continue
            
            self.buildroot_pkgs[pkg_name] = {}
            self.buildroot_pkgs[pkg_name]["source_name"] = pkg["source_name"]
            self.buildroot_pkgs[pkg_name]["required_by"] = pkg["required_by"]
            self.buildroot_pkgs[pkg_name]["required_by_srpms"] = build_dependencies[pkg_name]["required_by"]


        ### Initiate: self.buildroot_srpm_names
        for pkg_name, pkg in self.buildroot_pkgs.items():
            self.buildroot_srpm_names.add(pkg["source_name"])
        

        ### Initiate: self.buildroot_rpm_names
        self.buildroot_rpm_names = set(self.buildroot_pkgs.keys())
        

        ### Initiate: Other lists
        self.all_rpm_names = self.runtime_rpm_names.union(self.buildroot_rpm_names)
        self.all_srpm_names = self.runtime_srpm_names.union(self.buildroot_srpm_names)
        self.buildroot_only_rpm_names = self.buildroot_rpm_names.difference(self.runtime_rpm_names)
        self.buildroot_only_srpm_names = self.buildroot_srpm_names.difference(self.runtime_srpm_names)


        ### Initiate: self.pkg_entries
        for pkg_name in self.all_rpm_names:
            self.pkg_entries[pkg_name] = {}
            self.pkg_entries[pkg_name]["name"] = pkg_name
            for layer in range(0, self.MAX_LAYER + 1):
                for level in range(0, self.MAX_LEVEL + 1):
                    if layer == 0:
                        level_name = "level{}".format(level)
                        self.pkg_entries[pkg_name][level_name] = {}
                        self.pkg_entries[pkg_name][level_name]["workload_requirements"] = {}
                    else:
                        level_name = "level{}{}".format(layer, level)
                        self.pkg_entries[pkg_name][level_name] = {}
                        self.pkg_entries[pkg_name][level_name]["build_source_names"] = set()
        

        ### Initiate: self.srpm_entries
        for srpm_name in self.all_srpm_names:
            self.srpm_entries[srpm_name] = {}
            self.srpm_entries[srpm_name]["ownership"] = {}
            for layer in range(0, self.MAX_LAYER + 1):
                for level in range(0, self.MAX_LEVEL + 1):
                    if layer == 0:
                        level_name = "level{}".format(level)
                        self.srpm_entries[srpm_name]["ownership"][level_name] = {}
                    else:
                        level_name = "level{}{}".format(layer, level)
                        self.srpm_entries[srpm_name]["ownership"][level_name] = {}


        ### Initiate: self.component_maintainers
        for srpm_name in self.all_srpm_names:
            self.component_maintainers[srpm_name] = {}
            self.component_maintainers[srpm_name]["all"] = {}
            self.component_maintainers[srpm_name]["top_multiple"] = set()
            self.component_maintainers[srpm_name]["top"] = None



    def _pkg_relations_ids_to_names(self, pkg_relations):
        if not pkg_relations:
            return pkg_relations
        
        pkg_relations_names = {}

        for pkg_id, pkg in pkg_relations.items():
            pkg_name = pkg_id_to_name(pkg_id)

            pkg_relations_names[pkg_name] = {}
            pkg_relations_names[pkg_name]["required_by"] = set()

            for required_by_pkg_id in pkg["required_by"]:
                required_by_pkg_name = pkg_id_to_name(required_by_pkg_id)
                pkg_relations_names[pkg_name]["required_by"].add(required_by_pkg_name)
        
        return pkg_relations_names


    def _process_layer_zero_entries(self):
        # This is first done on an RPM level. Starts with level 0 == required,
        # assigns them based on who required them. Then moves on to level 1 == 1st
        # level depepdencies, and assigns them based on who pulled them in in
        # the above layer. And it goes deeper and deeper until MAX_LEVEL.
        # 
        # The second part of the function then takes this data from RPMs and
        # copies them over to their SRPMs. When multiple RPMs belong to a single
        # SRPM, it merges it.
        # 

        # 
        # Part 1: RPMs
        #  
    

        workload_ids = self.query.workloads_in_view(self.view_conf_id, None)

        for workload_id in workload_ids:
            workload = self.query.data["workloads"][workload_id]
            workload_conf_id = workload["workload_conf_id"]
            workload_conf = self.query.configs["workloads"][workload_conf_id]
            workload_maintainer = workload_conf["maintainer"]
            
            pkgs = self.query.workload_pkgs_id(workload_id)
            pkg_relations_ids = workload["pkg_relations"]

            pkg_relations = self._pkg_relations_ids_to_names(pkg_relations_ids)


            # Packages on level 0 == required
            level0_pkg_names = {}

            # This will initially hold all packages.
            # When figuring out levels, I'll process each package just once.
            # And for that I'll be removing them from this set as I go.
            remaining_pkg_names = set()
            
            for pkg in pkgs:
                pkg_name = pkg["name"]

                remaining_pkg_names.add(pkg_name)

                if "source_name" not in self.pkg_entries[pkg_name]:
                    self.pkg_entries[pkg_name]["source_name"] = pkg["source_name"]
        
                # Is this package level 1?
                if workload_id in pkg["q_required_in"]:
                    if workload_conf_id not in self.pkg_entries[pkg_name]["level0"]["workload_requirements"]:
                        self.pkg_entries[pkg_name]["level0"]["workload_requirements"][workload_conf_id] = set()
                    #level0_pkg_names.add(pkg_name)
                    if pkg_name not in level0_pkg_names:
                        level0_pkg_names[pkg_name] = set()
                    level0_pkg_names[pkg_name].add((None, pkg_name))
                    remaining_pkg_names.remove(pkg_name)
            

            # Initialize sets for all levels
            pkg_names_level = []
            pkg_names_level.append(level0_pkg_names)

            # Starting at level 1, because level 0 is already done (that's required packages)
            for current_level in range(1, self.MAX_LEVEL + 1):

                #1..
                #pkg_names_level.append(set())
                pkg_names_level.append({})

                for pkg_name in remaining_pkg_names.copy():
                    pkg = self.pkg_entries[pkg_name]

                    # is pkg required by higher_pkg_name (which is level 1)?
                    # (== is higher_pkg_name in a list of packages that pkg is required by?)
                    # then pkg is level 2
                    for higher_pkg_name in pkg_names_level[current_level - 1]:
                        if higher_pkg_name in pkg_relations[pkg_name]["required_by"]:
                            #pkg_names_level[current_level].add(pkg_name)
                            if pkg_name not in pkg_names_level[current_level]:
                                pkg_names_level[current_level][pkg_name] = set()
                            pkg_names_level[current_level][pkg_name].add((higher_pkg_name, pkg_name))

                            try:
                                remaining_pkg_names.remove(pkg_name)
                            except KeyError:
                                pass
            
            # Some might remain for weird reasons
            for pkg_name in remaining_pkg_names:
                #pkg_names_level[self.MAX_LEVEL].add(pkg_name)
                if pkg_name not in pkg_names_level[self.MAX_LEVEL]:
                    pkg_names_level[self.MAX_LEVEL][pkg_name] = set()


            for current_level in range(0, self.MAX_LEVEL + 1):
                level_name = "level{num}".format(num=str(current_level))

                for pkg_name in self.pkg_entries:
                    pkg = self.pkg_entries[pkg_name]

                    if pkg_name in pkg_names_level[current_level]:

                        if workload_conf_id not in self.pkg_entries[pkg_name][level_name]["workload_requirements"]:
                            self.pkg_entries[pkg_name][level_name]["workload_requirements"][workload_conf_id] = set()
                        
                        self.pkg_entries[pkg_name][level_name]["workload_requirements"][workload_conf_id].update(pkg_names_level[current_level][pkg_name])

        # 
        # Part 2: SRPMs
        # 

        for pkg_name, pkg in self.pkg_entries.items():
            if "source_name" not in pkg:
                continue

            source_name = pkg["source_name"]

            for current_level in range(0, self.MAX_LEVEL + 1):
                level_name = "level{num}".format(num=str(current_level))
                
                for workload_conf_id, pkg_names_requiring_this in pkg[level_name]["workload_requirements"].items():
                    maintainer = self.query.configs["workloads"][workload_conf_id]["maintainer"]
                    if maintainer not in self.srpm_entries[source_name]["ownership"][level_name]:
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer] = {}
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer]["workloads"] = {}
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_names"] = set()
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_count"] = 0
                    
                    if workload_conf_id not in self.srpm_entries[source_name]["ownership"][level_name][maintainer]["workloads"]:
                        self.srpm_entries[source_name]["ownership"][level_name][maintainer]["workloads"][workload_conf_id] = set()
                    
                    self.srpm_entries[source_name]["ownership"][level_name][maintainer]["workloads"][workload_conf_id].add(pkg_name)

                    self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_names"].update(pkg_names_requiring_this)
                    self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_count"] = len(self.srpm_entries[source_name]["ownership"][level_name][maintainer]["pkg_names"])




def perform_additional_analyses(query):

    for view_conf_id in query.configs["views"]:

        if "views" not in query.data:
            query.data["views"] = {}

        if not view_conf_id in query.data["views"]:
            query.data["views"][view_conf_id] = {}

        ownership_engine = OwnershipEngine(query)
        component_maintainers = ownership_engine.process_view(view_conf_id)

        query.data["views"][view_conf_id]["srpm_maintainers"] = component_maintainers
        query.data["views"][view_conf_id]["ownership_recommendations"] = ownership_engine.srpm_entries




###############################################################################
### Main ######################################################################
###############################################################################

def main():

    # measuring time of execution
    time_started = datetime_now_string()


    settings = load_settings()

    if settings["use_cache"]:
        configs = load_data("cache_configs.json")
        data = load_data("cache_data.json")
    else:
        configs = get_configs(settings)
        data = analyze_things(configs, settings)

        dump_data("cache_settings.json", settings)
        dump_data("cache_configs.json", configs)
        dump_data("cache_data.json", data)

    settings["global_refresh_time_started"] = datetime.datetime.now().strftime("%-d %B %Y %H:%M UTC")

    query = Query(data, configs, settings)

    perform_additional_analyses(query)

    # measuring time of execution
    time_analysis_time = datetime_now_string()

    generate_pages(query)
    generate_historic_data(query)

    #log("")
    #log("Repo split time!")
#
    #query.settings["allowed_arches"] = ["aarch64","ppc64le","s390x","x86_64"]
    #reposplit_configs = reposplit.get_configs(query.settings)
    #reposplit_data = reposplit.get_data(query)
    #reposplit_query = reposplit.Query(reposplit_data, reposplit_configs, query.settings)
    #reposplit_query.sort_out_pkgs()
    #reposplit.generate_pages(reposplit_query, include_content_resolver_breadcrumb=True)
    #reposplit.print_summary(reposplit_query)

    log("Done!")
    log("")

    # measuring time of execution
    time_ended = datetime_now_string()

    log("")
    log("=============================")
    log("Feedback Pipeline build done!")
    log("=============================")
    log("")
    log("  Started:       {}".format(time_started))
    log("  Analysis done: {}".format(time_analysis_time))
    log("  Finished:      {}".format(time_ended))
    log("")



def tests_to_be_made_actually_useful_at_some_point_because_this_is_terribble(query):


    print("")
    print("")
    print("")
    print("test test test")
    print("test test test")
    print("test test test")
    print("test test test")
    print("")
    print("")


    # does_workload_exist(self, workload_conf_id, env_conf_id, repo_id, arch):

    #   env-empty 
    #   env-minimal 
    #   label-eln-compose 
    #   label-fedora-31-all 
    #   label-fedora-rawhide-all 
    #   repo-fedora-31 
    #   repo-fedora-rawhide 
    #   view-eln-compose 
    #   workload-httpd 
    #   workload-nginx 
    #   x86_64
    #   aarch64

    print("Should be False:")
    print(query.workloads("mleko","mleko","mleko","mleko"))
    print("")

    print("Should be True:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31","x86_64"))
    print("")

    print("Should be False:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31","aarch64"))
    print("")

    print("Should be True:")
    print(query.workloads("workload-httpd","env-empty","repo-fedora-31",None))
    print("")

    print("Should be True:")
    print(query.workloads("workload-nginx",None, None, None))
    print("")

    print("Should be True:")
    print(query.workloads(None,"env-minimal",None,"x86_64"))
    print("")

    print("Should be True:")
    print(query.workloads(None,"env-minimal",None,"aarch64"))
    print("")

    print("Should be False:")
    print(query.workloads(None,"env-minimal","repo-fedora-31","x86_64"))
    print("")

    print("Should be False:")
    print(query.workloads(None,"env-minimal","repo-fedora-31","aarch64"))
    print("")

    print("----------")
    print("")
    print("")

    print("Should be 7:")
    print(len(query.workloads(None,None,None,None,list_all=True)))
    print("")

    print("Should be 3:")
    print(len(query.workloads(None,None,None,"aarch64",list_all=True)))
    print("")

    print("Should be 2:")
    print(len(query.workloads("workload-nginx",None,None,None,list_all=True)))
    print("")

    print("----------")
    print("")
    print("")

    print("Should be 2 workload-nginx:")
    for id in query.workloads("workload-nginx",None,None,None,list_all=True):
        print(id)
    print("")

    print("Should be all 7:")
    for id in query.workloads(None,None,None,None,list_all=True):
        print(id)
    print("")

    print("Should be all 6 rawhide:")
    for id in query.workloads(None,None,"repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("Should be all 2 empty rawhide:")
    for id in query.workloads(None,"env-empty","repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("Should be nothing:")
    for id in query.workloads("workload-nginx","env-empty","repo-fedora-rawhide",None,list_all=True):
        print(id)
    print("")

    print("----------")
    print("")
    print("")

    print("Should be env-empty:repo-fedora-31:x86_64")
    for id in query.envs_id("workload-httpd:env-empty:repo-fedora-31:x86_64", list_all=True):
        print(id)
    print("")

    print("Should be workload-httpd:env-empty:repo-fedora-31:x86_64")
    for id in query.workloads_id("workload-httpd:env-empty:repo-fedora-31:x86_64", list_all=True):
        print(id)
    print("")

    print("Should be two, workload-httpd:env-minimal:repo-fedora-rawhide:x86_64 and workload-nginx:...")
    for id in query.workloads_id("env-minimal:repo-fedora-rawhide:x86_64", list_all=True):
        print(id)
    print("")

    print("----------")
    print("")
    print("")

    print("Should be all 2 arches:")
    for id in query.workloads("workload-httpd",None, None,None,list_all=True,output_change="arches"):
        print(id)
    print("")

    print("Should be all 2 arches:")
    for id in query.workloads("workload-nginx",None, None,None,list_all=True,output_change="arches"):
        print(id)
    print("")

    print("Should be all 2 env_conf_ids:")
    for id in query.workloads("workload-httpd",None, None,None,list_all=True,output_change="env_conf_ids"):
        print(id)
    print("")

    print("Should be all 1 env_conf_id:")
    for id in query.workloads("workload-nginx",None, None,None,list_all=True,output_change="env_conf_ids"):
        print(id)
    print("")

    print("----------")
    print("")
    print("")

    print("Should be 104 packages:")
    pkgs = query.workload_pkgs("workload-nginx", "env-minimal", "repo-fedora-rawhide", "x86_64")
    print (len(pkgs))
    total = 0
    env = 0
    required = 0
    for pkg in pkgs:
        workload_id = "workload-nginx:env-minimal:repo-fedora-rawhide:x86_64"
        if workload_id in pkg["q_in"]:
            total += 1
        if workload_id in pkg["q_required_in"]:
            required += 1
        if workload_id in pkg["q_env_in"]:
            env +=1
    print("")
    print("Should be 104")
    print(total)
    print("Should be 22")
    print(env)
    print("Should be 1")
    print(required)
    print("")

    print("Should be 208 packages:")
    pkgs = query.workload_pkgs("workload-nginx", "env-minimal", "repo-fedora-rawhide", None)
    print (len(pkgs))
    total = 0
    env = 0
    required = 0
    for pkg in pkgs:
        workload_id = "workload-nginx:env-minimal:repo-fedora-rawhide:x86_64"
        if workload_id in pkg["q_in"]:
            total += 1
        if workload_id in pkg["q_required_in"]:
            required += 1
        if workload_id in pkg["q_env_in"]:
            env +=1
    print("")
    print("Should be 104")
    print(total)
    print("Should be 22")
    print(env)
    print("Should be 1")
    print(required)
    print("")
    print("")
    print("")

    print("----------")
    print("")
    print("")

    print("views!!!")
    print("")

    workload_ids = query.workloads_in_view("view-eln-compose", "x86_64")
    print("Should be 1:")
    print(len(workload_ids))
    print("")
    print("print should be one nginx")
    for workload_id in workload_ids:
        print(workload_id)


    print("")
    print("")
    print("Package Lists:")
    print("")
    print("")
    print("")
    package_ids1 = query.workload_pkgs_id("workload-httpd:env-empty:repo-fedora-rawhide:x86_64", output_change="ids")
    package_ids2 = query.workload_pkgs_id("workload-httpd:env-minimal:repo-fedora-rawhide:x86_64", output_change="ids")
    package_ids3 = query.workload_pkgs_id("workload-nginx:env-minimal:repo-fedora-rawhide:x86_64", output_change="ids")

    all_pkg_ids = set()

    all_pkg_ids.update(package_ids1)
    all_pkg_ids.update(package_ids2)
    all_pkg_ids.update(package_ids3)

    print(len(all_pkg_ids))

    pkg_ids = query.pkgs_in_view("view-eln-compose", "x86_64", output_change="ids")

    print(len(pkg_ids))





        # q_in          - set of workload_ids including this pkg
        # q_required_in - set of workload_ids where this pkg is required (top-level)
        # q_env_in      - set of workload_ids where this pkg is in env
        # size_text     - size in a human-readable format, like 6.5 MB



if __name__ == "__main__":
    main()
