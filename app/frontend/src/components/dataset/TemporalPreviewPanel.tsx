import { memo, useCallback, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { Map as MapGL, Source, Layer, NavigationControl } from '@vis.gl/react-maplibre';
import type { Map as MaplibreMap, MapLayerMouseEvent } from 'maplibre-gl';
import { CalendarDays, ChevronLeft, ChevronRight, Copy, ExternalLink, FolderOpen, MapPin } from 'lucide-react';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { apiFetch } from '@/api/client';
import { useTheme } from '@/components/theme-provider';
import { useBasemaps } from '@/hooks/use-settings';
import { useMapLocale } from '@/hooks/use-map-locale';
import { useQuicklook } from '@/components/maps/hooks/use-quicklook';
import { searchDatasets } from '@/api/search';
import {
  getThemeBasemap,
  toMaplibreStyle,
  FALLBACK_BASEMAP_STYLE_URL,
  FALLBACK_BASEMAP_STYLE_URL_DARK,
} from '@/lib/basemap-utils';
import { toFitBounds } from '@/lib/bbox';
import { buildTileTransformRequest } from '@/lib/tile-utils';
import { getEnvConfig } from '@/lib/env';
import { cn } from '@/lib/utils';
import type { OGCRecordResponse, SearchResponse } from '@/types/api';
import 'maplibre-gl/dist/maplibre-gl.css';
import '@/lib/maplibre-worker';

const MONTHS: Record<string, number> = {
  jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6,
  jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12,
};

function titleDate(title: string): number | null {
  const named = title.match(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[\s_./-]+(\d{4})\b/i);
  if (named) {
    return Date.UTC(Number(named[2]), (MONTHS[named[1].slice(0, 3).toLowerCase()] ?? 1) - 1, 1);
  }
  const numeric = title.match(/\b(19|20)\d{2}[-_./](\d{1,2})\b/);
  if (numeric) {
    const year = Number(numeric[0].slice(0, 4));
    const month = Number(numeric[2]);
    if (month >= 1 && month <= 12) return Date.UTC(year, month - 1, 1);
  }
  return null;
}

function seriesKey(title: string): string {
  return title
    .replace(/\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b/gi, ' ')
    .replace(/\b(19|20)\d{2}\b/g, ' ')
    .replace(/\b\d{1,2}\b/g, ' ')
    .replace(/[\s_./()[\],-]+/g, ' ')
    .trim()
    .toLowerCase();
}

function featureBbox(feature: OGCRecordResponse): [number, number, number, number] | null {
  if (feature.bbox && feature.bbox.length >= 4) {
    return [feature.bbox[0], feature.bbox[1], feature.bbox[2], feature.bbox[3]];
  }
  const geometry = feature.geometry;
  if (!geometry) return null;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const visit = (coords: unknown): void => {
    if (!Array.isArray(coords)) return;
    if (typeof coords[0] === 'number' && typeof coords[1] === 'number') {
      minX = Math.min(minX, coords[0]);
      minY = Math.min(minY, coords[1]);
      maxX = Math.max(maxX, coords[0]);
      maxY = Math.max(maxY, coords[1]);
      return;
    }
    coords.forEach(visit);
  };
  visit((geometry as GeoJSON.Geometry & { coordinates?: unknown }).coordinates);
  return Number.isFinite(minX) ? [minX, minY, maxX, maxY] : null;
}

function overlapRatio(a: [number, number, number, number], b: [number, number, number, number]): number {
  const ix = Math.max(0, Math.min(a[2], b[2]) - Math.max(a[0], b[0]));
  const iy = Math.max(0, Math.min(a[3], b[3]) - Math.max(a[1], b[1]));
  const inter = ix * iy;
  const areaA = Math.max(1e-9, (a[2] - a[0]) * (a[3] - a[1]));
  const areaB = Math.max(1e-9, (b[2] - b[0]) * (b[3] - b[1]));
  return inter / Math.min(areaA, areaB);
}

function sourcePath(description: string): string | null {
  const lib = description.match(/影像库路径[：:]\s*(.+)/);
  if (lib) return lib[1].trim();
  const source = description.match(/来源文件[：:]\s*(.+)/);
  return source ? source[1].trim() : null;
}

function labelFor(feature: OGCRecordResponse): string {
  const stamp = titleDate(feature.properties.title);
  if (stamp != null) {
    return new Date(stamp).toLocaleDateString('zh-CN', { year: 'numeric', month: 'short' });
  }
  return feature.properties.title;
}

interface TemporalPhase {
  id: string;
  title: string;
  date: number;
  bbox: [number, number, number, number] | null;
  path: string | null;
  quicklookVersion: string | null;
  feature: OGCRecordResponse;
}

function Thumb({ phase, selected, onSelect }: { phase: TemporalPhase; selected: boolean; onSelect: () => void }) {
  const { url, status } = useQuicklook(phase.id, 256, phase.quicklookVersion);
  return (
    <button
      type="button"
      onClick={onSelect}
      title={phase.path ?? phase.title}
      aria-pressed={selected}
      className={cn(
        'group/temporal flex w-[104px] shrink-0 flex-col gap-1 rounded-md border p-1 text-start transition-colors',
        selected ? 'border-primary bg-primary/5' : 'border-border/50 hover:border-border',
      )}
    >
      <span className="relative block h-[72px] w-full overflow-hidden rounded-sm bg-muted">
        {url ? (
          <img src={url} alt={phase.title} className="size-full object-cover dark:brightness-75" loading="lazy" />
        ) : (
          <span className="flex size-full items-center justify-center text-[10px] text-muted-foreground">
            {status === 'loading' ? '…' : '无预览'}
          </span>
        )}
        {selected && <span className="absolute inset-x-0 bottom-0 h-0.5 bg-primary" />}
      </span>
      <span className={cn('truncate text-[11px] font-medium', selected && 'text-primary')}>
        {labelFor(phase.feature)}
      </span>
    </button>
  );
}

const MemoThumb = memo(Thumb);

interface TemporalPreviewPanelProps {
  datasetId: string;
  title: string;
  bbox: [number, number, number, number];
  description?: string | null;
}

/**
 * Same-area time-series browser for raster datasets.
 *
 * Finds datasets whose footprint overlaps the current one (and whose title
 * shares the non-date part), draws their footprints on a map, and lets the
 * user step through dates by clicking the map or the thumbnail strip. The
 * selected phase shows its source file path and can be opened in the catalog.
 */
export function TemporalPreviewPanel({ datasetId, title, bbox, description }: TemporalPreviewPanelProps) {
  const { t } = useTranslation('dataset');
  const navigate = useNavigate();
  const { resolvedTheme } = useTheme();
  const { data: basemaps } = useBasemaps();
  const locale = useMapLocale();
  const [selectedId, setSelectedId] = useState(datasetId);
  const [hitIds, setHitIds] = useState<string[] | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const mapRef = useRef<MaplibreMap | null>(null);
  const localHost = ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname);

  const revealInExplorer = useCallback(async (phase: TemporalPhase) => {
    try {
      await apiFetch(`/datasets/${phase.id}/reveal`, { method: 'POST' });
      toast.success('已在文件夹中定位该文件');
    } catch {
      toast.error('无法定位文件（文件可能已被移动）');
    }
  }, []);

  const { data, isLoading } = useQuery<SearchResponse>({
    queryKey: ['temporal-phases', datasetId, bbox.join(',')],
    queryFn: () => searchDatasets({ bbox: bbox.join(','), limit: '100' }),
    staleTime: 60_000,
    retry: false,
  });

  const phases = useMemo<TemporalPhase[]>(() => {
    const currentKey = seriesKey(title);
    const rows = (data?.features ?? [])
      .filter((feature) => feature.properties.record_type === 'raster_dataset')
      .map((feature) => {
        const created = Date.parse(feature.properties.created ?? '');
        return {
          id: feature.id,
          title: feature.properties.title,
          date: titleDate(feature.properties.title) ?? (Number.isFinite(created) ? created : 0),
          bbox: featureBbox(feature),
          path: sourcePath(feature.properties.description ?? ''),
          quicklookVersion: (feature.properties as { quicklook_version?: string | null }).quicklook_version ?? null,
          feature,
        };
      })
      .filter((phase) => phase.bbox === null || overlapRatio(bbox, phase.bbox) > 0.4)
      .filter((phase) => phase.id === datasetId || seriesKey(phase.title) === currentKey || overlapRatio(bbox, phase.bbox ?? bbox) > 0.85);

    if (!rows.some((phase) => phase.id === datasetId)) {
      rows.push({
        id: datasetId,
        title,
        date: titleDate(title) ?? Date.now(),
        bbox,
        path: sourcePath(description ?? ''),
        quicklookVersion: null,
        feature: {
          type: 'Feature',
          id: datasetId,
          geometry: null,
          properties: { title, description: description ?? '' },
        } as unknown as OGCRecordResponse,
      });
    }
    // Earlier failed re-imports left same-title duplicates; one slot per date
    // is what makes the strip a timeline instead of a row of repeats.
    const byDate = new Map<string, TemporalPhase>();
    for (const phase of rows) {
      const key = `${phase.title.trim().toLowerCase()}|${phase.date}`;
      const existing = byDate.get(key);
      if (!existing || phase.id === datasetId) byDate.set(key, phase);
    }
    return [...byDate.values()].sort((a, b) => a.date - b.date);
  }, [data, datasetId, title, bbox, description]);

  const selected = phases.find((phase) => phase.id === selectedId) ?? phases.find((phase) => phase.id === datasetId) ?? phases[0];
  const selectedIndex = selected ? phases.indexOf(selected) : -1;

  const select = useCallback((id: string) => {
    setSelectedId(id);
    const phase = phases.find((row) => row.id === id);
    if (phase?.bbox && mapRef.current) {
      mapRef.current.fitBounds(toFitBounds(phase.bbox), { padding: 36, duration: 450, maxZoom: 14 });
    }
  }, [phases]);

  const step = useCallback((delta: number) => {
    if (selectedIndex < 0) return;
    const next = phases[selectedIndex + delta];
    if (next) select(next.id);
  }, [phases, selectedIndex, select]);

  const geojson = useMemo(() => ({
    type: 'FeatureCollection' as const,
    features: phases
      .filter((phase) => phase.feature.geometry)
      .map((phase) => ({
        type: 'Feature' as const,
        id: phase.id,
        geometry: phase.feature.geometry as GeoJSON.Geometry,
        properties: {
          datasetId: phase.id,
          selected: phase.id === selected?.id,
        },
      })),
  }), [phases, selected?.id]);

  const themeBasemap = getThemeBasemap(basemaps ?? [], resolvedTheme);
  const mapStyle = useMemo(
    () => themeBasemap
      ? toMaplibreStyle(themeBasemap.url, themeBasemap.attribution)
      : toMaplibreStyle(resolvedTheme === 'dark' ? FALLBACK_BASEMAP_STYLE_URL_DARK : FALLBACK_BASEMAP_STYLE_URL),
    [themeBasemap, resolvedTheme],
  );
  const tileBase = (getEnvConfig().TILE_BASE_URL || '/api').replace(/\/$/, '');

  if (isLoading) return null;
  if (phases.length < 2) return null;

  const handleMapClick = (event: MapLayerMouseEvent) => {
    const ids = Array.from(new Set(
      (event.features ?? [])
        .map((feature) => feature.properties?.datasetId)
        .filter((id): id is string => typeof id === 'string'),
    ));
    if (ids.length === 0) return;
    const order = new Map(phases.map((phase, index) => [phase.id, index]));
    ids.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
    setHitIds(ids);
    if (ids.length === 1) select(ids[0]);
  };

  return (
    <section
      aria-label={t('temporal.aria', { defaultValue: '多时相预览' })}
      className="rounded-lg border bg-background shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      data-testid="temporal-preview"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === 'ArrowLeft') { event.preventDefault(); step(-1); }
        if (event.key === 'ArrowRight') { event.preventDefault(); step(1); }
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
        <div className="flex items-center gap-2">
          <CalendarDays className="size-4 text-primary" />
          <span className="text-sm font-semibold">多时相预览</span>
          <span className="text-xs text-muted-foreground">{phases.length} 个时相</span>
        </div>
        <div className="flex items-center gap-1">
          <Button type="button" variant="ghost" size="icon" className="size-7" onClick={() => step(-1)}
            disabled={selectedIndex <= 0} aria-label="上一个时相">
            <ChevronLeft className="size-4" />
          </Button>
          <span className="min-w-[86px] text-center text-xs text-muted-foreground">
            {selectedIndex + 1} / {phases.length}
          </span>
          <Button type="button" variant="ghost" size="icon" className="size-7" onClick={() => step(1)}
            disabled={selectedIndex >= phases.length - 1} aria-label="下一个时相">
            <ChevronRight className="size-4" />
          </Button>
        </div>
      </div>

      <div className="grid gap-0 lg:grid-cols-[minmax(0,1.4fr)_minmax(260px,1fr)]">
        <div className="relative h-64 border-b lg:h-[300px] lg:border-b-0 lg:border-e">
          <MapGL
            mapStyle={mapStyle}
            style={{ width: '100%', height: '100%' }}
            locale={locale}
            interactive
            initialViewState={{ bounds: toFitBounds(bbox), fitBoundsOptions: { padding: 36 } }}
            onLoad={(event) => {
              mapRef.current = event.target;
              event.target.setTransformRequest(buildTileTransformRequest());
              event.target.fitBounds(toFitBounds(bbox), { padding: 36, duration: 0 });
              setMapReady(true);
            }}
            onClick={handleMapClick}
            interactiveLayerIds={['temporal-fill']}
          >
            <NavigationControl position="top-right" showCompass={false} />
            {mapReady && selected && (
              <Source
                key={selected.id}
                id={`temporal-raster-${selected.id}`}
                type="raster"
                tiles={[`${tileBase}/raster-tiles/${selected.id}/tiles/{z}/{x}/{y}.png?stretch=percentile`]}
                tileSize={256}
              >
                <Layer id={`temporal-raster-layer-${selected.id}`} type="raster" paint={{ 'raster-opacity': 0.92 }} />
              </Source>
            )}
            {geojson.features.length > 0 && (
              <Source id="temporal-footprints" type="geojson" data={geojson} promoteId="datasetId">
                <Layer
                  id="temporal-fill"
                  type="fill"
                  paint={{ 'fill-color': '#2563eb', 'fill-opacity': 0.16 }}
                />
                <Layer
                  id="temporal-selected-fill"
                  type="fill"
                  filter={['==', ['get', 'selected'], true]}
                  paint={{ 'fill-color': '#f97316', 'fill-opacity': 0.28 }}
                />
                <Layer
                  id="temporal-line"
                  type="line"
                  paint={{ 'line-color': '#1d4ed8', 'line-opacity': 0.75, 'line-width': 1.4 }}
                />
              </Source>
            )}
          </MapGL>
        </div>

        <div className="flex min-w-0 flex-col gap-3 p-3">
          {selected && (
            <>
              <div className="flex items-start gap-2">
                <MapPin className="mt-0.5 size-4 shrink-0 text-primary" />
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold" title={selected.title}>{selected.title}</div>
                  <div className="text-xs text-muted-foreground">{labelFor(selected.feature)}</div>
                </div>
              </div>
              {hitIds && hitIds.length > 1 && (
                <div className="rounded-md border border-primary/30 bg-primary/5 px-2 py-1.5">
                  <div className="text-[11px] text-muted-foreground">
                    此位置有 {hitIds.length} 个时相，点击选择：
                  </div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {hitIds.map((id) => {
                      const phase = phases.find((row) => row.id === id);
                      if (!phase) return null;
                      return (
                        <button
                          key={id}
                          type="button"
                          onClick={() => select(id)}
                          className={cn(
                            'rounded border px-1.5 py-0.5 text-[11px]',
                            id === selected?.id
                              ? 'border-primary bg-primary/10 text-primary'
                              : 'border-border/60 hover:border-border',
                          )}
                        >
                          {labelFor(phase.feature)}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
              <div className="min-w-0 rounded-md border border-border/50 bg-muted/25 px-2 py-1.5">
                <div className="text-[11px] text-muted-foreground">对应文件</div>
                <div className="break-all font-mono text-[11px] leading-4" title={selected.path ?? ''}>
                  {selected.path ?? '（未记录来源路径）'}
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" size="sm" onClick={() => navigate(`/datasets/${selected.id}`)}
                  disabled={selected.id === datasetId}>
                  <ExternalLink className="me-1 size-3.5" />
                  {selected.id === datasetId ? '当前数据集' : '打开该数据集'}
                </Button>
                {selected.path && (
                  <Button type="button" size="sm" variant="outline"
                    onClick={() => {
                      void navigator.clipboard.writeText(selected.path!).then(
                        () => toast.success('路径已复制'),
                        () => toast.error('复制失败'),
                      );
                    }}>
                    <Copy className="me-1 size-3.5" />
                    复制路径
                  </Button>
                )}
                {selected.path && localHost && (
                  <Button type="button" size="sm" variant="outline"
                    onClick={() => void revealInExplorer(selected)}>
                    <FolderOpen className="me-1 size-3.5" />
                    在文件夹中定位
                  </Button>
                )}
              </div>
              <p className="text-[11px] leading-4 text-muted-foreground">
                点击地图上的色块可定位到该时相的文件；← / → 切换时相。
              </p>
            </>
          )}
        </div>
      </div>

      <div className="flex gap-2 overflow-x-auto border-t px-3 py-2" role="listbox" aria-label="时相列表">
        {phases.map((phase) => (
          <MemoThumb
            key={phase.id}
            phase={phase}
            selected={phase.id === selected?.id}
            onSelect={() => select(phase.id)}
          />
        ))}
      </div>
    </section>
  );
}
