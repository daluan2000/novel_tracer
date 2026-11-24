import { describe, expect, it } from 'vitest'
import { graphSections, parseRoute, routeHash } from './navigation'
describe('workspace navigation', () => {
  it.each(graphSections)('restores the $label subpage directly', ({ id }) => {
    expect(parseRoute(`#/graph/${id}`)).toEqual({ page: 'graph', graphSection: id })
    expect(routeHash({ page: 'graph', graphSection: id })).toBe(`#/graph/${id}`)
  })
  it('maps the old graph address and unknown graph sections to exploration', () => {
    expect(routeHash(parseRoute('#/graph'))).toBe('#/graph/explore')
    expect(routeHash(parseRoute('#/graph/unknown'))).toBe('#/graph/explore')
    expect(parseRoute('#/unknown').page).toBe('agent')
  })
})
