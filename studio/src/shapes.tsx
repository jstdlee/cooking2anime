// One custom tldraw shape, "c2a-panel": a movable, resizable card that hosts a live panel.
import {
  BaseBoxShapeUtil,
  HTMLContainer,
  type RecordProps,
  T,
  type TLShape,
} from 'tldraw'
import { ClipsPanel, KeysPanel, OutputPanel, SamplesPanel, StylePanel } from './panels'

export const PANEL = 'c2a-panel'
export type PanelKind = 'keys' | 'style' | 'samples' | 'clips' | 'output'

declare module 'tldraw' {
  export interface TLGlobalShapePropsMap {
    [PANEL]: { w: number; h: number; kind: string }
  }
}

export type PanelShape = TLShape<typeof PANEL>

const BODIES: Record<PanelKind, () => React.JSX.Element> = {
  keys: KeysPanel,
  style: StylePanel,
  samples: SamplesPanel,
  clips: ClipsPanel,
  output: OutputPanel,
}

export class PanelShapeUtil extends BaseBoxShapeUtil<PanelShape> {
  static override type = PANEL
  static override props: RecordProps<PanelShape> = { w: T.number, h: T.number, kind: T.string }

  getDefaultProps(): PanelShape['props'] {
    return { w: 600, h: 400, kind: 'keys' }
  }

  override canEdit() {
    return false
  }

  override canResize() {
    return true
  }

  override hideRotateHandle() {
    return true
  }

  component(shape: PanelShape) {
    const Body = BODIES[shape.props.kind as PanelKind] ?? KeysPanel
    return (
      <HTMLContainer
        className={`c2a-panel kind-${shape.props.kind}`}
        style={{ width: shape.props.w, height: shape.props.h, pointerEvents: 'all' }}
        data-testid={`panel-${shape.props.kind}`}
      >
        <Body />
      </HTMLContainer>
    )
  }

  override getIndicatorPath(shape: PanelShape) {
    const path = new Path2D()
    path.roundRect(0, 0, shape.props.w, shape.props.h, 12)
    return path
  }
}

export const LAYOUT: { kind: PanelKind; x: number; y: number; w: number; h: number }[] = [
  { kind: 'keys', x: 0, y: 0, w: 1460, h: 360 },
  { kind: 'style', x: 0, y: 400, w: 480, h: 640 },
  { kind: 'samples', x: 520, y: 400, w: 940, h: 640 },
  { kind: 'clips', x: 0, y: 1080, w: 1460, h: 520 },
  { kind: 'output', x: 0, y: 1640, w: 960, h: 680 },
]
