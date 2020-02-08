class RepoWrangler(object):

  def __init__(self, dry_run=False, verbose=0, progress=None, quiet=False):
    self.n_repos = 0
    self.n_updated = 0
    self.n_new = 0
    self.n_dirty = 0
    self.dry_run = dry_run
    self.verbose = verbose or 0
    self.quiet = quiet
    self.progress = progress if progress else Progress()
    self.lock = threading.Lock()

  def get_github_list(self, list_url, message):
    self.progress.status(message)

    def progress_callback(n):
      self.progress.status("{} ({})".format(message, n))

    return get_github_list(list_url, progress_callback=progress_callback)

  def list_gists(self, user, pattern=None):
    list_url = 'https://api.github.com/users/{}/gists'.format(user)
    message = "Fetching list of {}'s gists from GitHub...".format(user)
    gists = self.get_github_list(list_url, message)
    if pattern:
      # TBH this is of questionable utility, but maybe you want to clone
      # a single gist and so you can pass --pattern=id-of-that-gist
      gists = (g for g in gists if fnmatch.fnmatch(g['id'], pattern))
    # other possibilities for filtering:
    # - exclude private gists (if g['public'])
    return sorted(map(Repo.from_gist, gists), key=attrgetter('name'))

  def list_repos(self, user=None, organization=None, pattern=None,
                 include_archived=False, include_forks=False,
                 include_private=True, include_disabled=True):
    if organization and not user:
      owner = organization
      list_url = 'https://api.github.com/orgs/{}/repos'.format(owner)
    elif user and not organization:
      owner = user
      list_url = 'https://api.github.com/users/{}/repos'.format(owner)
    else:
      raise ValueError('specify either user or organization, not both')

    message = "Fetching list of {}'s repositories from GitHub...".format(
        owner)

    # User repositories default to sort=full_name, org repositories default
    # to sort=created.  In theory we don't care because we will sort the
    # list ourselves, but in the future I may want to start cloning in
    # parallel with the paginated fetching.  This requires the sorting to
    # happen before pagination, i.e. on the server side, as I want to
    # process the repositories alphabetically (both for aesthetic reasons,
    # and in order for --start-from to be useful).
    list_url += '?sort=full_name'

    repos = self.get_github_list(list_url, message)
    if not include_archived:
      repos = (r for r in repos if not r['archived'])
    if not include_forks:
      repos = (r for r in repos if not r['fork'])
    if not include_private:
      repos = (r for r in repos if not r['private'])
    if not include_disabled:
      repos = (r for r in repos if not r['disabled'])
    # other possibilities for filtering:
    # - exclude template repos (if not r['is_template']), once that feature
    #   is out of beta
    if pattern:
      repos = (r for r in repos if fnmatch.fnmatch(r['name'], pattern))
    return sorted(map(Repo.from_repo, repos), key=attrgetter('name'))

  def repo_task(self, repo):
    item = self.progress.item("+ {name}".format(name=repo.name))
    task = RepoTask(repo, item, self, self.task_finished)
    return task

  @synchronized
  def task_finished(self, task):
    self.n_repos += 1
    self.n_new += task.new
    self.n_updated += task.updated
    self.n_dirty += task.dirty
