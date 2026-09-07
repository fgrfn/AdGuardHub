/**
 * Which build is running, and where it came from.
 *
 * The version used to be a line at the bottom of Settings, which is the one page
 * you are not on when you ask "did my update actually land?". It belongs under
 * every page instead.
 *
 * The destinations follow what a self-hosted tool's footer is actually used for,
 * which "Source & issues" ran together into one link that was neither. AdGuard
 * Home's own footer — the one sitting next to this in most people's tabs — is
 * version, homepage, report an issue; Pi-hole links each version number at its
 * release. So: the running build points at its own tag, so it answers "what is
 * in this version" rather than "what is on main"; GitHub goes to the repository;
 * reporting a problem goes straight to the new-issue form rather than to a list
 * to read.
 */

import { api } from '../api/client'
import { useResource } from '../hooks/useApi'
import { useT } from '../i18n'
import { ISSUES_URL, REPO_URL, releaseUrl } from '../repo'
import { IconGitHub } from './icons'

export function Footer() {
  const t = useT()
  const health = useResource(() => api.health())
  const version = health.data?.version

  return (
    <footer className="footer">
      {version ? (
        <a href={releaseUrl(version)} target="_blank" rel="noreferrer noopener">
          {t('AdGuardHub {version}', { version })}
        </a>
      ) : (
        <span>AdGuardHub</span>
      )}

      {/* The separator travels with the destination it precedes rather than
          sitting between them as its own flex item: on a narrow screen a
          standalone one is left dangling at the end of a wrapped line.
          It belongs to the wrapper and not to the link, because
          `text-decoration` cannot be switched off in a descendant — drawn
          inside the anchor, the dot was underlined along with the label on
          hover. */}
      <span className="footer-dest">
        <a href={REPO_URL} target="_blank" rel="noreferrer noopener">
          <IconGitHub />
          GitHub
        </a>
      </span>

      <span className="footer-dest">
        <a href={ISSUES_URL} target="_blank" rel="noreferrer noopener">
          {t('Report an issue')}
        </a>
      </span>
    </footer>
  )
}
