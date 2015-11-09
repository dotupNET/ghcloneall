#!/usr/bin/python3

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.request
from operator import itemgetter

import requests
import requests_cache


__author__ = 'Marius Gedminas <marius@gedmin.as>'
__licence__ = 'MIT'
__url__ = 'https://github.com/mgedmin/cloneall'
__version__ = '1.3.dev0'


DEFAULT_ORGANIZATION = 'ZopeFoundation'


class Error(Exception):
    """An error that is not a bug in this script."""


def get_json_and_headers(url):
    """Perform HTTP GET for a URL, return deserialized JSON and headers.

    Returns a tuple (json_data, headers) where headers is something dict-like.
    """
    r = requests.get(url)
    if 400 <= r.status_code < 500:
        raise Error("Failed to fetch {}:\n{}".format(url, r.json()['message']))
    return r.json(), r.headers


def get_github_list(url, batch_size=100, progress_callback=None):
    """Perform (a series of) HTTP GETs for a URL, return deserialized JSON.

    Format of the JSON is documented at
    http://developer.github.com/v3/repos/#list-organization-repositories

    Supports batching (which GitHub indicates by the presence of a Link header,
    e.g. ::

        Link: <https://api.github.com/resource?page=2>; rel="next",
              <https://api.github.com/resource?page=5>; rel="last"

    """
    # API documented at http://developer.github.com/v3/#pagination
    res, headers = get_json_and_headers('{}?per_page={}'.format(
                                                url, batch_size))
    page = 1
    while 'rel="next"' in headers.get('Link', ''):
        page += 1
        if progress_callback:
            progress_callback(len(res))
        more, headers = get_json_and_headers('{}?page={}&per_page={}'.format(
                                                    url, page, batch_size))
        res += more
    return res


def synchronized(method):
    def wrapper(self, *args, **kw):
        with self.lock:
            return method(self, *args, **kw)
    return wrapper



class Progress(object):
    """A progress bar.

    There are two parts of progress output:

    - a scrolling list of items
    - a progress bar (or status message) at the bottom

    These are controlled by the following API methods:

    - status(msg) replaces the progress bar with a status message
    - clear() clears the progress bar/status message
    - set_total(n) defines how many items there will be in total
    - item(text) shows an item and updates the progress bar
    - update(extra_text) updates the last item (and highlights it in a
      different color)
    - finish(msg) clear the progress bar/status message and print a summary

    """

    progress_bar_format = '[{bar}] {cur}/{total}'
    bar_width = 20

    t_cursor_up = '\033[%dA'
    t_cursor_down = '\033[%dB'
    t_insert_lines = '\033[%dL'
    t_reset = '\033[m'
    t_green = '\033[32m'
    t_red = '\033[31m'

    def __init__(self, stream=sys.stdout):
        self.stream = stream
        self.last_status = ''  # so we know how many characters to erase
        self.cur = self.total = 0
        self.items = []
        self.lock = threading.Lock()

    def status(self, message):
        """Replace the status message."""
        self.clear()
        if message:
            self.stream.write('\r')
            self.stream.write(message)
            self.stream.write('\r')
            self.stream.flush()
            self.last_status = message

    def clear(self):
        """Clear the status message."""
        if self.last_status:
            self.stream.write('\r{}\r'.format(' ' * len(self.last_status.rstrip())))
            self.stream.flush()
            self.last_status = ''

    def finish(self, msg=''):
        """Clear the status message and print a summary.

        Differs from status(msg) in that it leaves the cursor on a new line
        and cannot be cleared.
        """
        self.clear()
        if msg:
            print(msg, file=self.stream)

    def progress(self):
        self.status(self.format_progress_bar(self.cur, self.total))

    def format_progress_bar(self, cur, total):
        return self.progress_bar_format.format(
            cur=cur, total=total, bar=self.bar(cur, total))

    def scale(self, range, cur, total):
        return range * cur // max(total, 1)

    def bar(self, cur, total):
        n = min(self.scale(self.bar_width, cur, total), self.bar_width)
        return ('=' * n).ljust(self.bar_width)

    def set_limit(self, total):
        """Specify the expected total number of items.

        E.g. if you set_limit(10), this means you expect to call item() ten
        times.
        """
        self.total = total
        self.progress()

    @synchronized
    def item(self, msg=''):
        """Show an item and update the progress bar."""
        item = self.Item(self, msg, len(self.items))
        self.items.append(item)
        if msg:
            self.clear()
            print(msg, file=self.stream)
        self.cur += 1
        self.progress()
        return item

    @synchronized
    def update_item(self, item):
        n = sum(i.height for i in self.items[item.idx:])
        self.stream.write(''.join([
            self.t_cursor_up % n,
            item.color,
            item.msg,
            item.reset,
            '\n' * n,
        ]))
        self.stream.flush()

    @synchronized
    def extra_info(self, item, lines):
        n = sum(i.height for i in self.items[item.idx + 1:])
        if n:
            self.stream.write(self.t_cursor_up % n)
        self.stream.write(self.t_insert_lines % len(lines))
        for indent, color, line, reset in lines:
            self.stream.write(''.join([indent, color, line, reset, '\n']))
        for i in self.items[item.idx + 1:]:
            self.stream.write(''.join([i.color, i.msg, i.reset, '\n']))
            for indent, color, line, reset in i.extra_info_lines:
                self.stream.write(''.join([indent, color, line, reset, '\n']))
        self.progress()

    class Item(object):
        def __init__(self, progress, msg, idx):
            self.progress = progress
            self.msg = msg
            self.idx = idx
            self.extra_info_lines = []
            self.color = ''
            self.reset = ''

        @property
        def height(self):
            return 1 + len(self.extra_info_lines)

        def update(self, msg):
            """Update the last shown item and highlight it."""
            self.color = self.progress.t_green
            self.reset = self.progress.t_reset
            self.msg += msg
            self.progress.update_item(self)

        def extra_info(self, msg, color='', reset='', indent='    '):
            """Print some extra information."""
            lines = [(indent, color, line, reset) for line in msg.splitlines()]
            if not lines:
                return
            self.extra_info_lines += lines
            self.progress.extra_info(self, lines)

        def error_info(self, msg):
            """Print some extra information about an error."""
            self.extra_info(msg, color=self.progress.t_red, reset=self.progress.t_reset)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        self.clear()
        if exc_type is KeyboardInterrupt:
            print('Interrupted', file=self.stream)


class RepoWrangler(object):

    def __init__(self, dry_run=False, verbose=0, progress=None):
        self.n_repos = 0
        self.n_updated = 0
        self.n_new = 0
        self.n_dirty = 0
        self.dry_run = dry_run
        self.verbose = verbose or 0
        self.progress = progress if progress else Progress()
        self.lock = threading.Lock()

    def list_repos(self, organization):
        self.progress.status('Fetching list of {} repositories from GitHub...'.format(organization))
        def progress_callback(n):
            self.progress.status('Fetching list of {} repositories from GitHub... ({})'.format(organization, n))
        list_url = 'https://api.github.com/orgs/{}/repos'.format(organization)
        repos = get_github_list(list_url, progress_callback=progress_callback)
        return sorted(repos, key=itemgetter('name'))

    def process_task(self, repo):
        item = self.progress.item("+ {name}".format(**repo))
        task = RepoTask(repo, item, self, self.task_finished)
        return task

    @synchronized
    def task_finished(self, task):
        self.n_repos += 1
        self.n_new += task.new
        self.n_updated += task.updated
        self.n_dirty += task.dirty


class RepoTask(object):

    def __init__(self, repo, progress_item, options, finished_callback):
        self.repo = repo
        self.progress_item = progress_item
        self.options = options
        self.finished_callback = finished_callback
        self.updated = False
        self.new = False
        self.dirty = False

    def repo_dir(self, repo):
        return repo['name']

    def repo_url(self, repo):
        # use repo['git_url'] for anonymous checkouts, but they'e slower
        # (at least as long as you use SSH connection multiplexing)
        return repo['ssh_url']

    def decode(self, output):
        return output.decode('UTF-8', 'replace')

    def branch_name(self, head):
        if head.startswith('refs/'):
            head = head[len('refs/'):]
        if head.startswith('heads/'):
            head = head[len('heads/'):]
        return head

    def pretty_command(self, args):
        if self.options.verbose:
            return ' '.join(args)
        else:
            return ' '.join(args[:2])  # 'git diff' etc.

    def call(self, args, **kwargs):
        """Call a subprocess and return its exit code.

        The subprocess is expected to produce no output.  If any output is
        seen, it'll be displayed as an error.
        """
        p = subprocess.Popen(args, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, **kwargs)
        output, _ = p.communicate()
        retcode = p.wait()
        if output:
            self.progress_item.error_info(self.decode(output))
            self.progress_item.error_info('{command} exited with {rc}'.format(command=self.pretty_command(args),
                                                                         rc=retcode))
        return retcode

    def check_call(self, args, **kwargs):
        """Call a subprocess.

        The subprocess is expected to produce no output.  If any output is
        seen, it'll be displayed as an error.

        The subprocess is expected to return exit code 0.  If it returns
        non-zero, that'll be displayed as an error.
        """
        p = subprocess.Popen(args, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, **kwargs)
        output, _ = p.communicate()
        retcode = p.wait()
        if retcode != 0:
            self.progress_item.update(' (failed)')
        if output or retcode != 0:
            self.progress_item.error_info(self.decode(output))
            self.progress_item.error_info('{command} exited with {rc}'.format(command=self.pretty_command(args),
                                                                         rc=retcode))

    def check_output(self, args, **kwargs):
        """Call a subprocess and return its standard output code.

        The subprocess is expected to produce no output on stderr.  If any
        output is seen, it'll be displayed as an error.

        The subprocess is expected to return exit code 0.  If it returns
        non-zero, that'll be displayed as an error.
        """
        p = subprocess.Popen(args, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, **kwargs)
        stdout, stderr = p.communicate()
        retcode = p.wait()
        if retcode != 0:
            self.progress_item.error_info(self.decode(stderr))
            self.progress_item.error_info('{command} exited with {rc}'.format(command=self.pretty_command(args),
                                                                         rc=retcode))
        return self.decode(stdout)

    def run(self):
        dir = self.repo_dir(self.repo)
        if os.path.exists(dir):
            self.update(self.repo, dir)
            self.verify(self.repo, dir)
        else:
            self.clone(self.repo, dir)
        if self.finished_callback:
            self.finished_callback(self)

    def clone(self, repo, dir):
        if not self.options.dry_run:
            self.progress_item.update(' (new)')
            url = self.repo_url(repo)
            self.check_call(['git', 'clone', '-q', url])
        self.new = True

    def update(self, repo, dir):
        if not self.options.dry_run:
            old_sha = self.get_current_commit(dir)
            self.check_call(['git', 'pull', '-q', '--ff-only'], cwd=dir)
            new_sha = self.get_current_commit(dir)
            if old_sha != new_sha:
                self.progress_item.update(' (updated)')
                self.updated = True

    def verify(self, repo, dir):
        if self.has_local_changes(dir):
            self.progress_item.update(' (local changes)')
            self.dirty = True
        if self.has_staged_changes(dir):
            self.progress_item.update(' (staged changes)')
            self.dirty = True
        if self.has_local_commits(dir):
            self.progress_item.update(' (local commits)')
            self.dirty = True
        branch = self.get_current_branch(dir)
        if branch != 'master':
            self.progress_item.update(' (not on master)')
            if self.options.verbose >= 2:
                self.progress_item.extra_info('branch: {}'.format(branch))
            self.dirty = True
        if self.options.verbose:
            remote_url = self.get_remote_url(dir)
            if remote_url != repo['ssh_url'] and remote_url + '.git' != repo['ssh_url']:
                self.progress_item.update(' (wrong remote url)')
                if self.options.verbose >= 2:
                    self.progress_item.extra_info('remote: {}'.format(remote_url))
                self.dirty = True
        if self.options.verbose:
            unknown_files = self.get_unknown_files(dir)
            if unknown_files:
                self.progress_item.update(' (unknown files)')
                if self.options.verbose >= 2:
                    if self.options.verbose < 3 and len(unknown_files) > 10:
                        unknown_files[10:] = ['(and %d more)' % (len(unknown_files) - 10)]
                    self.progress_item.extra_info('\n'.join(unknown_files))
                self.dirty = True

    def has_local_changes(self, dir):
        # command borrowed from /usr/lib/git-core/git-sh-prompt
        return self.call(['git', 'diff', '--no-ext-diff', '--quiet', '--exit-code'], cwd=dir) != 0

    def has_staged_changes(self, dir):
        # command borrowed from /usr/lib/git-core/git-sh-prompt
        return self.call(['git', 'diff-index', '--cached', '--quiet', 'HEAD', '--'], cwd=dir) != 0

    def has_local_commits(self, dir):
        return self.check_output(['git', 'rev-list', '@{u}..'], cwd=dir) != ''

    def get_current_commit(self, dir):
        return self.check_output(['git', 'describe', '--always', '--dirty'], cwd=dir)

    def get_current_head(self, dir):
        return self.check_output(['git', 'symbolic-ref', 'HEAD'], cwd=dir).strip()

    def get_current_branch(self, dir):
        return self.branch_name(self.get_current_head(dir))

    def get_remote_url(self, dir):
        return self.check_output(['git', 'ls-remote', '--get-url'], cwd=dir).strip()

    def get_unknown_files(self, dir):
        # command borrowed from /usr/lib/git-core/git-sh-prompt
        return self.check_output(['git', 'ls-files', '--others', '--exclude-standard', '--', ':/*'], cwd=dir).splitlines()


class SequentialJobQueue(object):

    def add(self, task):
        task.run()

    def finish(self):
        pass


class ConcurrentJobQueue(object):

    def __init__(self, concurrency=2):
        self.jobs = []
        self.concurrency = concurrency

    def add(self, task):
        while len(self.jobs) >= self.concurrency:
            self.jobs.pop(0).join()
        t = threading.Thread(target=task.run)
        t.start()
        self.jobs.append(t)

    def finish(self):
        while self.jobs:
            self.jobs.pop(0).join()


def main():
    parser = argparse.ArgumentParser(
        description="Clone/update all organization repositories from GitHub")
    parser.add_argument('--version', action='version',
                        version="%(prog)s version " + __version__)
    parser.add_argument('-c', '--concurrency', type=int, default=4,
                        help="set concurrency level")
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help="don't pull/clone, just print what would be done")
    parser.add_argument('-v', '--verbose', action='count',
                        help="perform additional checks")
    parser.add_argument('--start-from', metavar='REPO',
                        help='skip all repositories that come before REPO alphabetically')
    parser.add_argument('--organization', default=DEFAULT_ORGANIZATION,
                        help='specify the GitHub organization (default: %s)' % DEFAULT_ORGANIZATION)
    parser.add_argument('--http-cache', default='.httpcache', metavar='DBNAME',
                        # .sqlite will be appended automatically
                        help='cache HTTP requests on disk in an sqlite database (default: .httpcache)')
    parser.add_argument('--no-http-cache', action='store_false', dest='http_cache',
                        help='disable HTTP disk caching')
    args = parser.parse_args()
    if args.http_cache:
        requests_cache.install_cache(args.http_cache,
                                     backend='sqlite',
                                     expire_after=300)

    with Progress() as progress:
        wrangler = RepoWrangler(dry_run=args.dry_run, verbose=args.verbose, progress=progress)
        repos = wrangler.list_repos(args.organization)
        progress.set_limit(len(repos))
        if args.concurrency < 2:
            queue = SequentialJobQueue()
        else:
            queue = ConcurrentJobQueue(args.concurrency)
        for repo in repos:
            if args.start_from and repo['name'] < args.start_from:
                progress.item()
                continue
            task = wrangler.process_task(repo)
            queue.add(task)
        queue.finish()
        progress.finish("{0.n_repos} repositories: {0.n_updated} updated, {0.n_new} new, {0.n_dirty} dirty.".format(wrangler))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
