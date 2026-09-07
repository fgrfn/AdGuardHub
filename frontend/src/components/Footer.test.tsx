/**
 * The footer's one piece of logic: which URL the version links to.
 *
 * A wrong guess here is a 404 on the project's own page — the link is only worth
 * having if it lands.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { I18nProvider } from '../i18n/provider'
import { ISSUES_URL, REPO_URL, releaseUrl } from '../repo'
import { Footer } from './Footer'

describe('releaseUrl', () => {
  it('points a released build at its own tag', () => {
    expect(releaseUrl('v0.2.0')).toBe(`${REPO_URL}/releases/tag/v0.2.0`)
  })

  it('adds the v the tags carry when the version came without one', () => {
    expect(releaseUrl('0.2.0')).toBe(`${REPO_URL}/releases/tag/v0.2.0`)
  })

  it('keeps a pre-release suffix rather than truncating to the release', () => {
    expect(releaseUrl('v1.0.0-rc.1')).toBe(`${REPO_URL}/releases/tag/v1.0.0-rc.1`)
  })

  it('sends anything that is not a version to the repository', () => {
    // "dev" is what an unreleased build reports, and there is no tag for it.
    expect(releaseUrl('dev')).toBe(REPO_URL)
    expect(releaseUrl('')).toBe(REPO_URL)
  })
})

describe('Footer', () => {
  it('links to the project and the issue form before the version has loaded', () => {
    // `/api/health` is not stubbed here, so this is the first paint. The links
    // that help someone whose hub is misbehaving must not be waiting on a
    // request that may never answer — which is exactly when they are wanted.
    render(
      <I18nProvider>
        <Footer />
      </I18nProvider>,
    )
    const hrefs = screen.getAllByRole('link').map((link) => link.getAttribute('href'))
    expect(hrefs).toContain(REPO_URL)
    expect(hrefs).toContain(ISSUES_URL)
  })

  it('sends "report an issue" to the form rather than to the list to read', () => {
    expect(ISSUES_URL).toBe(`${REPO_URL}/issues/new`)
  })

  it('keeps the separator outside the link it belongs to', () => {
    // The dot is a ::before, which jsdom does not render, so the check is on
    // where it is attached. That is the whole of the bug: drawn inside the
    // anchor it was underlined along with the label on hover, and
    // text-decoration cannot be switched off in a descendant — the line comes
    // from the ancestor. Keeping it on a wrapper is the fix, and this is what
    // would quietly undo it.
    const { container } = render(
      <I18nProvider>
        <Footer />
      </I18nProvider>,
    )

    const separators = container.querySelectorAll('.footer-dest')
    expect(separators).toHaveLength(2)
    for (const holder of separators) {
      expect(holder.tagName).not.toBe('A')
      expect(holder.querySelector('a')).not.toBeNull()
    }
  })
})
