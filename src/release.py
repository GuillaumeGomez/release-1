#!/bin/python3

from contextlib import contextmanager
# pip3 install datetime
import datetime
from github import Github
from os import listdir, sep as os_sep
from os.path import isdir, isfile, join
from my_toml import TomlHandler
from utils import clone_repo, compare_versions, exec_command, exec_command_and_print_error
from utils import get_features, get_file_content, post_content, write_error, write_into_file
from utils import write_msg
import consts
import errno
import getopt
import time
import shutil
import sys
import tempfile


CRATES_VERSION = {}
PULL_REQUESTS = []
SEARCH_INDEX = []
SEARCH_INDEX_BEFORE = []
SEARCH_INDEX_AFTER = []


class UpdateType:
    MAJOR = 0
    MEDIUM = 1
    MINOR = 2

    def create_from_string(s):
        s = s.lower()
        if s == 'major':
            return UpdateType.MAJOR
        elif s == 'medium':
            return UpdateType.MEDIUM
        elif s == 'minor':
            return UpdateType.MINOR
        return None


@contextmanager
def TemporaryDirectory():
    name = tempfile.mkdtemp()
    try:
        yield name
    finally:
        try:
            shutil.rmtree(name)
        except OSError as e:
            # if the directory has already been removed, no need to raise an error
            if e.errno != errno.ENOENT:
                raise


def update_version(version, update_type, section_name, place_type="section"):
    version_split = version.replace('"', '').split('.')
    if len(version_split) != 3:
        # houston, we've got a problem!
        write_error('Invalid version in {} "{}": {}'.format(place_type, section_name, version))
        return None
    if update_type == UpdateType.MINOR:
        version_split[update_type] = str(int(version_split[update_type]) + 1)
    elif update_type == UpdateType.MEDIUM:
        version_split[update_type] = str(int(version_split[update_type]) + 1)
        version_split[UpdateType.MINOR] = '0'
    else:
        i = 0
        for c in version_split[update_type]:
            if c >= '0' and c <= '9':
                break
            i += 1
        if i >= len(c):
            return None
        s = version_split[update_type][i:]
        version_split[update_type] = '{}{}'.format(version_split[update_type][:i], str(int(s) + 1))
        version_split[UpdateType.MEDIUM] = '0'
        version_split[UpdateType.MINOR] = '0'
    return '"{}"'.format('.'.join(version_split))


def check_and_update_version(entry, update_type, dependency_name, versions_update):
    if entry.startswith('"') or entry.startswith("'"):
        return update_version(entry, update_type, dependency_name, place_type="dependency")
    # get version and update it
    entry = [e.strip() for e in entry.split(',')]
    dic = {}
    for part in entry:
        if part.startswith('{'):
            part = part[1:].strip()
        if part.endswith('}'):
            part = part[:-1].strip()
        part = [p.strip() for p in part.split('=')]
        dic[part[0]] = part[1]
        if part[0] == 'version':
            old_version = part[1]
            new_version = update_version(old_version, update_type, dependency_name,
                                         place_type="dependency")
            if new_version is None:
                return None
            versions_update.append({'dependency_name': dependency_name,
                                    'old_version': old_version,
                                    'new_version': new_version})
            dic[part[0]] = '"{}"'.format(new_version)
    return '{{{}}}'.format(', '.join(['{} = {}'.format(entry, dic[entry]) for entry in dic]))


def find_crate(crate_name):
    for entry in consts.CRATE_LIST:
        if entry['crate'] == crate_name:
            return True
    return False


def update_crate_version(repo_name, crate_name, crate_dir_path, temp_dir, specified_crate):
    file_path = join(join(join(temp_dir, repo_name), crate_dir_path), "Cargo.toml")
    output = file_path.replace(temp_dir, "")
    if output.startswith('/'):
        output = output[1:]
    write_msg('=> Updating crate versions for {}'.format(file_path))
    content = get_file_content(file_path)
    if content is None:
        return False
    toml = TomlHandler(content)
    for section in toml.sections:
        if section.name == 'package':
            section.set('version', CRATES_VERSION[crate_name])
        elif specified_crate is not None:
            continue
        elif section.name.startswith('dependencies.') and find_crate(section.name[13:]):
            if specified_crate is None and section.name[13:] not in CRATES_VERSION:
                input('"{}" dependency not found in versions for crate "{}"...'
                      .format(section.name[13:], crate_name))
                continue
            section.set('version', CRATES_VERSION[section.name[13:]])
        elif section.name == 'dependencies':
            for entry in section.entries:
                if find_crate(entry['key']):
                    section.set(entry['key'], CRATES_VERSION[entry['key']])
    result = write_into_file(file_path, str(toml))
    write_msg('=> {}: {}'.format(output.split(os_sep)[-2],
                                 'Failure' if result is False else 'Success'))
    return result


def update_repo_version(repo_name, crate_name, crate_dir_path, temp_dir, update_type,
                        no_update):
    file_path = join(join(join(temp_dir, repo_name), crate_dir_path), "Cargo.toml")
    output = file_path.replace(temp_dir, "")
    if output.startswith('/'):
        output = output[1:]
    write_msg('=> Updating versions for {}'.format(file_path))
    content = get_file_content(file_path)
    if content is None:
        return False
    toml = TomlHandler(content)
    versions_update = []
    for section in toml.sections:
        if (section.name == 'package' or
                (section.name.startswith('dependencies.') and find_crate(section.name[13:]))):
            version = section.get('version', None)
            if version is None:
                continue
            new_version = None
            if no_update is False:
                new_version = update_version(version, update_type, section.name)
            else:
                new_version = version
            if new_version is None:
                return False
            # Print the status directly if it's the crate's version.
            if section.name == 'package':
                write_msg('\t{}: {} => {}'.format(output.split(os_sep)[-2], version, new_version))
                CRATES_VERSION[crate_name] = new_version
            else:  # Otherwise add it to the list to print later.
                versions_update.append({'dependency_name': section.name[13:],
                                        'old_version': version,
                                        'new_version': new_version})
            section.set('version', new_version)
        elif section.name == 'dependencies':
            for entry in section.entries:
                if find_crate(entry):
                    new_version = check_and_update_version(section.entries[entry],
                                                           update_type,
                                                           entry)
                    section.set(entry, new_version)
    for up in versions_update:
        write_msg('\t{}: {} => {}'.format(up['dependency_name'], up['old_version'],
                                          up['new_version']))
    out = str(toml)
    if not out.endswith("\n"):
        out += '\n'
    result = True
    if no_update is False:
        # We only write into the file if we're not just getting the crates version.
        result = write_into_file(file_path, out)
        write_msg('=> {}: {}'.format(output.split(os_sep)[-2],
                                     'Failure' if result is False else 'Success'))
    return result


def commit_and_push(repo_name, temp_dir, commit_msg, target_branch):
    commit(repo_name, temp_dir, commit_msg)
    push(repo_name, temp_dir, target_branch)


def commit(repo_name, temp_dir, commit_msg):
    repo_path = join(temp_dir, repo_name)
    command = ['bash', '-c', 'cd {} && git commit . -m "{}"'.format(repo_path, commit_msg)]
    if not exec_command_and_print_error(command):
        input("Fix the error and then press ENTER")


def push(repo_name, temp_dir, target_branch):
    repo_path = join(temp_dir, repo_name)
    command = ['bash', '-c', 'cd {} && git push -f origin HEAD:{}'.format(repo_path, target_branch)]
    if not exec_command_and_print_error(command):
        input("Fix the error and then press ENTER")


def add_to_commit(repo_name, temp_dir, files_to_add):
    repo_path = join(temp_dir, repo_name)
    command = ['bash', '-c', 'cd {} && git add {}'
               .format(repo_path, ' '.join(['"{}"'.format(f) for f in files_to_add]))]
    if not exec_command_and_print_error(command):
        input("Fix the error and then press ENTER")


def revert_changes(repo_name, temp_dir, files):
    repo_path = join(temp_dir, repo_name)
    command = ['bash', '-c',
               'cd {} && git checkout -- {}'.format(repo_path,
                                                    ' '.join(['"{}"'.format(f) for f in files]))]
    if not exec_command_and_print_error(command):
        input("Fix the error and then press ENTER")


def checkout_target_branch(repo_name, temp_dir, target_branch):
    repo_path = join(temp_dir, repo_name)
    command = ['bash', '-c', 'cd {} && git checkout {}'.format(repo_path, target_branch)]
    if not exec_command_and_print_error(command):
        input("Fix the error and then press ENTER")


def get_last_commit_date(repo_name, temp_dir):
    repo_path = join(temp_dir, repo_name)
    success, out, err = exec_command(['bash', '-c',
                                      'cd {} && git log --format=%at --no-merges -n 1'.format(
                                          repo_path)
                                      ])
    return (success, out, err)


def merging_branches(repo_name, temp_dir, merge_branch):
    repo_path = join(temp_dir, repo_name)
    command = ['bash', '-c', 'cd {} && git merge "origin/{}"'.format(repo_path, merge_branch)]
    if not exec_command_and_print_error(command):
        input("Fix the error and then press ENTER")


def publish_crate(repository, crate_dir_path, temp_dir, crate_name):
    write_msg('=> publishing crate {}'.format(crate_name))
    path = join(join(temp_dir, repository), crate_dir_path)
    # In case we needed to fix bugs, we checkout to crate branch before publishing crate.
    command = ['bash', '-c', 'cd {} && git checkout crate && cargo publish'.format(path)]
    if not exec_command_and_print_error(command):
        input("Something bad happened! Try to fix it and then press ENTER to continue...")
    write_msg('> crate {} has been published'.format(crate_name))


def create_tag_and_push(tag_name, repository, temp_dir):
    path = join(temp_dir, repository)
    command = ['bash', '-c', 'cd {} && git tag "{}" && git push origin "{}"'.format(path,
                                                                                    tag_name,
                                                                                    tag_name)]
    if not exec_command_and_print_error(command):
        input("Something bad happened! Try to fix it and then press ENTER to continue...")


def push_tag(tag_name, repository, temp_dir):
    path = join(temp_dir, repository)
    command = ['bash', '-c', 'cd {} && git push origin "{}"'.format(path, tag_name)]
    if not exec_command_and_print_error(command):
        input("Something bad happened! Try to fix it and then press ENTER to continue...")


def create_pull_request(repo_name, from_branch, target_branch, token, add_to_list=True):
    r = post_content('{}/repos/{}/{}/pulls'.format(consts.GH_API_URL, consts.ORGANIZATION,
                                                   repo_name),
                     token,
                     {'title': '[release] merging {} into {}'.format(from_branch, target_branch),
                      'body': 'cc @GuillaumeGomez @EPashkin @sdroege',
                      'base': target_branch,
                      'head': from_branch,
                      'maintainer_can_modify': True})
    if r is None:
        write_error("Pull request from {repo}/{from_b} to {repo}/{target} couldn't be created. You "
                    "need to do it yourself... (url provided at the end)"
                    .format(repo=repo_name,
                            from_b=from_branch,
                            target=target_branch))
        input("Press ENTER once done to continue...")
        PULL_REQUESTS.append('|=> "{}/{}/{}/compare/{}...{}?expand=1"'
                             .format(consts.GITHUB_URL,
                                     consts.ORGANIZATION,
                                     repo_name,
                                     target_branch,
                                     from_branch))
    else:
        write_msg("===> Pull request created: {}".format(r['html_url']))
        if add_to_list is True:
            PULL_REQUESTS.append('> {}'.format(r['html_url']))


def update_badges(repo_name, temp_dir, specified_crate):
    path = join(join(temp_dir, repo_name), "_data/crates.json")
    content = get_file_content(path)
    current = None
    out = []
    for line in content.split("\n"):
        if line.strip().startswith('"name": "'):
            current = line.split('"name": "')[-1].replace('",', '')
            if specified_crate is not None and current != specified_crate:
                current = None
        elif line.strip().startswith('"max_version": "') and current is not None:
            version = line.split('"max_version": "')[-1].replace('"', '').replace(',', '')
            out.append(line.replace('": "{}"'.format(version),
                                    '": {}'.format(CRATES_VERSION[current])) + '\n')
            current = None
            continue
        out.append(line + '\n')
    return write_into_file(path, ''.join(out).replace('\n\n', '\n'))


def cleanup_doc_repo(temp_dir):
    path = join(temp_dir, consts.DOC_REPO)
    dirs = ' '.join(['"{}"'.format(join(path, f)) for f in listdir(path)
                     if isdir(join(path, f)) and f.startswith('.') is False])
    command = ['bash', '-c', 'cd {} && rm -rf {}'.format(path, dirs)]
    if not exec_command_and_print_error(command):
        input("Couldn't clean up docs! Try to fix it and then press ENTER to continue...")


def build_docs(repo_name, temp_dir, extra_path, crate_name):
    path = join(join(temp_dir, repo_name), extra_path)
    features = get_features(join(path, 'Cargo.toml'))
    # We can't add "--no-deps" argument to cargo doc, otherwise we lose links to items of
    # other crates...
    #
    # Also, we run "cargo update" in case the lgpl-docs repository has been updated (so we get the
    # last version).
    command = ['bash', '-c',
               'cd {} && cargo update && cargo rustdoc --no-default-features --features "{}" -- \
                -Z unstable-options --disable-minification'.format(path, features)]
    if not exec_command_and_print_error(command):
        input("Couldn't generate docs! Try to fix it and then press ENTER to continue...")
    doc_folder = join(path, 'target/doc')
    try:
        file_list = ' '.join(['"{}"'.format(f) for f in listdir(doc_folder)
                              if isfile(join(doc_folder, f))])
    except Exception as e:
        write_error('Error occured in build docs: {}'.format(e))
        input("It seems like the \"{}\" folder doesn't exist. Try to fix it then press ENTER..."
              .format(doc_folder))
    # Copy documentation files
    command = ['bash', '-c',
               'cd {} && cp -r "{}" {} "{}"'
               .format(doc_folder,
                       crate_name.replace('-', '_'),
                       file_list,
                       join(temp_dir, consts.DOC_REPO))]
    if not exec_command_and_print_error(command):
        input("Couldn't copy docs! Try to fix it and then press ENTER to continue...")
    # Copy source files
    destination = "{}/src".format(join(temp_dir, consts.DOC_REPO))
    command = ['bash', '-c',
               'cd {} && mkdir -p "{}" && cp -r "src/{}" "{}/"'
               .format(doc_folder,
                       destination,
                       crate_name.replace('-', '_'),
                       destination)]
    if not exec_command_and_print_error(command):
        input("Couldn't copy doc source files! Try to fix it and then press ENTER to continue...")
    search_index = join(path, 'target/doc/search-index.js')
    lines = get_file_content(search_index).split('\n')
    before = True
    fill_extras = len(SEARCH_INDEX_BEFORE) == 0
    found = False
    for line in lines:
        if line.startswith('searchIndex['):
            before = False
            # We need to be careful in here if we're in a sys repository (which should never be the
            # case!).
            if line.startswith('searchIndex["{}"]'.format(crate_name.replace('-', '_'))):
                SEARCH_INDEX.append(line)
                found = True
        elif fill_extras is True:
            if before is True:
                SEARCH_INDEX_BEFORE.append(line)
            else:
                SEARCH_INDEX_AFTER.append(line)
    if found is False:
        input("Couldn't find \"{}\" in `{}`!\nTry to fix it and then press ENTER to continue..."
              .format(crate_name.replace('-', '_'), search_index))


def end_docs_build(temp_dir):
    path = join(temp_dir, consts.DOC_REPO)
    revert_changes(consts.DOC_REPO, temp_dir,
                   ['COPYRIGHT.txt', 'LICENSE-APACHE.txt', 'LICENSE-MIT.txt'])
    try:
        lines = get_file_content(join(path, 'search-index.js')).split('\n')
        with open(join(path, 'search-index.js'), 'w') as f:
            f.write('\n'.join(SEARCH_INDEX_BEFORE))
            f.write('\n'.join(SEARCH_INDEX))
            f.write('\n'.join(SEARCH_INDEX_AFTER))
        command = ['bash', '-c',
                   'cd minifier && cargo run --release -- "{}"'.format(path)]
        if not exec_command_and_print_error(command):
            input("Couldn't run minifier! Try to fix it and then press ENTER to continue...")
        add_to_commit(consts.DOC_REPO, temp_dir, ['.'])
    except Exception as e:
        print('An exception occured in "end_docs_build": {}'.format(e))
        input("Press ENTER to continue...")


def write_merged_prs(merged_prs, contributors, repo_url):
    content = ''
    for pr in merged_prs:
        if pr.title.startswith('[release] '):
            continue
        if pr.author not in contributors:
            contributors.append(pr.author)
        content += ' * [{}]({}/pull/{})\n'.format(pr.title.replace('<', '&lt;').replace('>', '&gt;'),
                                                  repo_url,
                                                  pr.number)
    return content + '\n'


def build_blog_post(repositories, temp_dir, token):
    content = '''---
layout: post
author: {}
title: {}
categories: [front, crates]
date: {}
---

* Write intro here *

### Changes

For the interested ones, here is the list of the merged pull requests:

'''.format(input('Enter author name: '), input('Enter title: '),
           time.strftime("%Y-%m-%d %H:00:00 +0000"))
    contributors = []
    git = Github(token)
    oldest_date = None
    for repo in repositories:
        checkout_target_branch(repo, temp_dir, "crate")
        success, out, err = get_last_commit_date(repo, temp_dir)
        if not success:
            write_msg("Couldn't get PRs for '{}': {}".format(repo, err))
            continue
        max_date = datetime.date.fromtimestamp(int(out))
        if oldest_date is None or max_date < oldest_date:
            oldest_date = max_date
        write_msg("Gettings merged PRs from {}...".format(repo))
        merged_prs = git.get_pulls(repo, consts.ORGANIZATION, 'closed', max_date, only_merged=True)
        write_msg("=> Got {} merged PRs".format(len(merged_prs)))
        if len(merged_prs) < 1:
            continue
        repo_url = '{}/{}/{}'.format(consts.GITHUB_URL, consts.ORGANIZATION, repo)
        content += '[{}]({}):\n\n'.format(repo, repo_url)
        content += write_merged_prs(merged_prs, contributors, repo_url)

    write_msg("Gettings merged PRs from gir...")
    merged_prs = git.get_pulls('gir', consts.ORGANIZATION, 'closed', oldest_date, only_merged=True)
    write_msg("=> Got {} merged PRs".format(len(merged_prs)))
    if len(merged_prs) > 0:
        repo_url = '{}/{}/{}'.format(consts.GITHUB_URL, consts.ORGANIZATION, 'gir')
        content += ('All this was possible thanks to the [gtk-rs/gir]({}) project as well:\n\n'
                    .format(repo_url))
        content += write_merged_prs(merged_prs, contributors, repo_url)

    content += 'Thanks to all of our contributors for their (awesome!) work on this release:\n\n'
    content += '\n'.join([' * [@{}]({}/{})'.format(contributor, consts.GITHUB_URL, contributor)
                          for contributor in contributors])
    content += '\n'

    file_name = join(join(temp_dir, consts.BLOG_REPO),
                     '_posts/{}-new-release.md'.format(time.strftime("%Y-%m-%d")))
    try:
        with open(file_name, 'w') as outfile:
            outfile.write(content)
            write_msg('New blog post written into "{}".'.format(file_name))
        add_to_commit(consts.BLOG_REPO, temp_dir, [file_name])
        commit(consts.BLOG_REPO, temp_dir, "Add new blog post")
    except Exception as e:
        write_error('build_blog_post failed: {}'.format(e))
        write_msg('\n=> Here is the blog post content:\n{}\n<='.format(content))


def generate_new_tag(repository, temp_dir, specified_crate):
    versions = {}
    version = None

    # We make a new tag for every crate:
    #
    # * If it is a "sys" crate, then we add its name to the tag
    # * If not, then we just keep its version number
    for crate in consts.CRATE_LIST:
        if crate['repository'] == repository:
            tag_name = CRATES_VERSION[crate['crate']]
            if crate['crate'].endswith('-sys') or crate['crate'].endswith('-sys-rs'):
                tag_name = '{}-{}'.format(crate['crate'], tag_name)
            write_msg('==> Creating new tag "{}" for repository "{}"...'.format(tag_name,
                                                                                repository))
            create_tag_and_push(tag_name, repository, temp_dir)


def update_doc_content_repository(repositories, temp_dir, token, no_push):
    if clone_repo(consts.DOC_CONTENT_REPO, temp_dir) is False:
        input('Try to fix the problem then press ENTER to continue...')
    repo_path = join(temp_dir, consts.DOC_CONTENT_REPO)
    for repo in repositories:
        if repo.get("doc", True) is False:
            continue
        path = join(temp_dir, repo['repository'])
        command = ['bash', '-c',
                   'cd {} && make doc && mv vendor.md {}'.format(path,
                                                                 join(repo_path, repo['crate']))]
        if not exec_command_and_print_error(command):
            input("Fix the error and then press ENTER")
    write_msg('Committing "{}" changes...'.format(consts.DOC_CONTENT_REPO))
    commit(consts.DOC_CONTENT_REPO, temp_dir, "Update vendor files")
    if no_push is False:
        push(consts.DOC_CONTENT_REPO, temp_dir, consts.MASTER_TMP_BRANCH)

    # We always make minor releases in here, no need for a more important one considering we don't
    # change the API.
    if update_repo_version(consts.DOC_CONTENT_REPO, consts.DOC_CONTENT_REPO, "",
                           temp_dir, UpdateType.MINOR, False) is False:
        write_error('The update for the "{}" crate failed...'.format(consts.DOC_CONTENT_REPO))
        input('Fix the error and then press ENTER')
    commit(consts.DOC_CONTENT_REPO, temp_dir, "Update version")
    if no_push is False:
        push(consts.DOC_CONTENT_REPO, temp_dir, consts.MASTER_TMP_BRANCH)
        create_pull_request(consts.DOC_CONTENT_REPO, consts.MASTER_TMP_BRANCH, "master", token,
                            False)
        input('All done with the "{}" update: please merge the PR then press ENTER so the \
               publication can performed...'.format(consts.DOC_CONTENT_REPO))
        publish_crate(consts.DOC_CONTENT_REPO, "", temp_dir, consts.DOC_CONTENT_REPO)
        write_msg('Ok all done! We can move forward now!')
    else:
        write_msg('All with "{}", you still need to publish a new version if you want the changes \
                   to be taken into account'.format(consts.DOC_CONTENT_REPO))


def start(update_type, token, no_push, doc_only, specified_crate, badges_only, tags_only):
    write_msg('=> Creating temporary directory...')
    with TemporaryDirectory() as temp_dir:
        write_msg('Temporary directory created in "{}"'.format(temp_dir))
        write_msg('=> Cloning the repositories...')
        repositories = []
        for crate in consts.CRATE_LIST:
            if specified_crate is not None and crate['crate'] != specified_crate:
                continue
            if crate["repository"] not in repositories:
                repositories.append(crate["repository"])
                if clone_repo(crate["repository"], temp_dir) is False:
                    write_error('Cannot clone the "{}" repository...'.format(crate["repository"]))
                    return
        if len(repositories) < 1:
            write_msg('No crate "{}" found. Aborting...'.format(specified_crate))
            return
        if doc_only is False:
            if clone_repo(consts.BLOG_REPO, temp_dir, depth=1) is False:
                write_error('Cannot clone the "{}" repository...'.format(consts.BLOG_REPO))
                return
        if clone_repo(consts.DOC_REPO, temp_dir, depth=1) is False:
            write_error('Cannot clone the "{}" repository...'.format(consts.DOC_REPO))
            return
        write_msg('Done!')

        if doc_only is False:
            write_msg('=> Updating [master] crates version...')
            for crate in consts.CRATE_LIST:
                if specified_crate is not None and crate['crate'] != specified_crate:
                    continue
                if update_repo_version(crate["repository"], crate["crate"], crate["path"],
                                       temp_dir, update_type, badges_only or tags_only) is False:
                    write_error('The update for the "{}" crate failed...'.format(crate["crate"]))
                    return
            write_msg('Done!')

            if badges_only is False and tags_only is False:
                write_msg('=> Committing{} to the "{}" branch...'
                          .format(" and pushing" if no_push is False else "",
                                  consts.MASTER_TMP_BRANCH))
                for repo in repositories:
                    commit(repo, temp_dir, "Update versions [ci skip]")
                    if no_push is False:
                        push(repo, temp_dir, consts.MASTER_TMP_BRANCH)
                write_msg('Done!')

                if no_push is False:
                    write_msg('=> Creating PRs on master branch...')
                    for repo in repositories:
                        create_pull_request(repo, consts.MASTER_TMP_BRANCH, "master", token)
                    write_msg('Done!')

                write_msg('=> Building blog post...')
                build_blog_post(repositories, temp_dir, token)
                write_msg('Done!')

        write_msg('=> Checking out "crate" branches')
        for repo in repositories:
            checkout_target_branch(repo, temp_dir, "crate")
        write_msg('Done!')

        if doc_only is False and badges_only is False and tags_only is False:
            write_msg('=> Merging "master" branches into "crate" branches...')
            for repo in repositories:
                merging_branches(repo, temp_dir, "master")
            write_msg('Done!')

            write_msg('=> Updating [crate] crates version...')
            for crate in consts.CRATE_LIST:
                if specified_crate is not None and crate['crate'] != specified_crate:
                    continue
                if update_crate_version(crate["repository"], crate["crate"], crate["path"],
                                        temp_dir, specified_crate) is False:
                    write_error('The update for the "{}" crate failed...'.format(crate["crate"]))
                    return
            write_msg('Done!')

            write_msg('=> Committing{} to the "{}" branch...'
                      .format(" and pushing" if no_push is False else "",
                              consts.CRATE_TMP_BRANCH))
            for repo in repositories:
                commit(repo, temp_dir, "Update versions [ci skip]")
                if no_push is False:
                    push(repo, temp_dir, consts.CRATE_TMP_BRANCH)
            write_msg('Done!')

            if no_push is False:
                write_msg('=> Creating PRs on crate branch...')
                for repo in repositories:
                    create_pull_request(repo, consts.CRATE_TMP_BRANCH, "crate", token)
                write_msg('Done!')

                write_msg('+++++++++++++++')
                write_msg('++ IMPORTANT ++')
                write_msg('+++++++++++++++')
                write_msg('Almost everything has been done. Take a deep breath, check for opened '
                          'pull requests and once done, we can move forward!')
                write_msg("\n{}\n".format('\n'.join(PULL_REQUESTS)))
                PULL_REQUESTS.append('=============')
                input('Press ENTER to continue...')
                write_msg('=> Publishing crates...')
                for crate in consts.CRATE_LIST:
                    if specified_crate is not None and crate['crate'] != specified_crate:
                        continue
                    publish_crate(crate["repository"], crate["path"], temp_dir, crate['crate'])
                write_msg('Done!')

                write_msg('=> Creating PR for examples repository')
                create_pull_request("examples", "pending", "master", token)
                write_msg('Done!')

        if no_push is False and doc_only is False and badges_only is False:
            write_msg("=> Generating tags...")
            for repo in repositories:
                generate_new_tag(repo, temp_dir, specified_crate)
            write_msg('Done!')

        if badges_only is False and tags_only is False:
            # It might seem counter-intuitive but in case we didn't update other repositories,
            # there is no interest into publishing a new version of "lgpl-docs".
            input("remove me")
            if doc_only is False:
                update_doc_content_repository(repositories, temp_dir, token, no_push)
            write_msg('=> Preparing doc repo (too much dark magic in here urg)...')
            cleanup_doc_repo(temp_dir)
            write_msg('Done!')

            write_msg('=> Building docs...')
            for crate in consts.CRATE_LIST:
                if crate['crate'] == 'gtk-test':
                    continue
                write_msg('-> Building docs for {}...'.format(crate['crate']))
                build_docs(crate['repository'], temp_dir, crate['path'],
                           crate.get('doc_name', crate['crate']))
            end_docs_build(temp_dir)
            write_msg('Done!')

            write_msg('=> Committing{} docs to the "{}" branch...'
                      .format(" and pushing" if no_push is False else "",
                              consts.CRATE_TMP_BRANCH))
            commit(consts.DOC_REPO, temp_dir, "Regen docs")
            if no_push is False:
                push(consts.DOC_REPO, temp_dir, consts.CRATE_TMP_BRANCH)
                create_pull_request(consts.DOC_REPO, consts.CRATE_TMP_BRANCH, "gh-pages", token)
                write_msg("New pull request(s):\n\n{}\n".format('\n'.join(PULL_REQUESTS)))
            write_msg('Done!')

        if doc_only is False and tags_only is False:
            write_msg('=> Updating blog...')
            if update_badges(consts.BLOG_REPO, temp_dir, specified_crate) is False:
                write_error("Error when trying to update badges...")
            elif no_push is False:
                commit_and_push(consts.BLOG_REPO, temp_dir, "Update versions",
                                consts.MASTER_TMP_BRANCH)
                create_pull_request(consts.BLOG_REPO, consts.MASTER_TMP_BRANCH, "master", token)
            write_msg('Done!')

        write_msg('Seems like most things are done! Now remains:')
        write_msg(" * Check generated docs for all crates (don't forget to enable features!).")
        input('Press ENTER to leave (once done, the temporary directory "{}" will be destroyed)'
              .format(temp_dir))


def write_help():
    write_msg("release.py accepts the following options:")
    write_msg("")
    write_msg(" * -h | --help                  : display this message")
    write_msg(" * -t <token> | --token=<token> : give the github token")
    write_msg(" * -m <mode> | --mode=<mode>    : give the update type (MINOR|MEDIUM|MAJOR)")
    write_msg(" * --no-push                    : performs all operations but doesn't push anything")
    write_msg(" * --doc-only                   : only builds documentation")
    write_msg(" * -c <crate> | --crate=<crate> : only update the given crate (for test purpose mainly)")
    write_msg(" * --badges-only                : only update the badges on the website")
    write_msg(" * --tags-only                  : only create new tags")


def main(argv):
    try:
        opts, args = getopt.getopt(argv,
                                   "ht:m:c:",
                                   ["help", "token=", "mode=", "no-push", "doc-only", "crate",
                                    "badges-only", "tags-only"])
    except getopt.GetoptError:
        write_help()
        sys.exit(2)

    token = None
    mode = None
    no_push = False
    doc_only = False
    specified_crate = None
    badges_only = False
    tags_only = False

    for opt, arg in opts:
        if opt in ('-h', '--help'):
            write_help()
            sys.exit(0)
        elif opt in ("-t", "--token"):
            token = arg
        elif opt in ("-m", "--mode"):
            mode = UpdateType.create_from_string(arg)
            if mode is None:
                write_error('{}: Invalid update type received. Accepted values: '
                            '(MINOR|MEDIUM|MAJOR)'.format(opt))
                sys.exit(3)
        elif opt in ("--no-push"):
            no_push = True
        elif opt in ("--doc-only"):
            doc_only = True
        elif opt in ("--badges-only"):
            badges_only = True
        elif opt in ('-c', '--crate'):
            specified_crate = arg
        elif opt in ('--tags-only'):
            tags_only = True
        else:
            write_msg('"{}": unknown option'.format(opt))
            write_msg('Use "-h" or "--help" to see help')
            sys.exit(0)
    if token is None and no_push is False:
        write_error('Missing token argument.')
        sys.exit(4)
    if mode is None and doc_only is False and badges_only is False and tags_only is False:
        write_error('Missing update type argument.')
        sys.exit(5)
    start(mode, token, no_push, doc_only, specified_crate, badges_only, tags_only)


# Beginning of the script
if __name__ == "__main__":
    main(sys.argv[1:])
