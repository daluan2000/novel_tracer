import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import FlowGraph from './FlowGraph.vue'

describe('FlowGraph', () => {
  it('shows active, completed and failed node states with text equivalents', async () => {
    const wrapper = mount(FlowGraph, {
      props: { activeNode: 'researcher', visitedNodes: ['planner'], failed: false },
    })

    expect(wrapper.findAll('.is-done')).toHaveLength(1)
    expect(wrapper.find('.is-active').text()).toContain('Researcher')
    expect(wrapper.text()).toContain('active')

    await wrapper.setProps({ failed: true })
    expect(wrapper.find('.is-failed').text()).toContain('Researcher')
    expect(wrapper.text()).toContain('failed')
  })
})
