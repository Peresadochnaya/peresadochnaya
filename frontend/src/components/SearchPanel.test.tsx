import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { SearchPanel } from './SearchPanel';
import { DummyConfig, Point } from '../types/api';

vi.mock('../api/client', () => ({
  api: {
    getTicketProfiles: vi.fn(async () => []),
    getSuggestions: vi.fn(async () => ({ items: [] })),
    geocode: vi.fn(async () => ({ items: [] })),
  },
}));

const points: { A: Point; B: Point } = {
  A: { lat: 59.93, lon: 30.31 },
  B: { lat: 59.94, lon: 30.32 },
};

const dummyConfig: DummyConfig = {
  suggest: true,
  geocode: true,
  routes: true,
  stops: true,
  departures: true,
};

const baseProps = {
  labelA: 'Старт',
  labelB: 'Финиш',
  pointA: points.A,
  pointB: points.B,
  onSelectPlace: vi.fn(),
  onClearPoint: vi.fn(),
  onSwapPoints: vi.fn(),
  onSearch: vi.fn(),
  dummyConfig,
  onUpdateDummyConfig: vi.fn(),
  onClearPoints: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('SearchPanel compact view', () => {
  it('shows filters and the search button in the full view', () => {
    render(<SearchPanel {...baseProps} />);

    expect(screen.getByText('Все запросы Dummy (демо)')).toBeTruthy();
    expect(screen.getByText('Виды транспорта:')).toBeTruthy();
    expect(screen.getByDisplayValue('Старт')).toBeTruthy();
    expect(screen.getByDisplayValue('Финиш')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Найти маршруты' })).toBeTruthy();
  });

  it('keeps only the A/B fields and search button in compact view', () => {
    render(<SearchPanel {...baseProps} />);

    fireEvent.click(screen.getByRole('button', { name: 'Компактно' }));

    expect(screen.queryByText('Все запросы Dummy (демо)')).toBeNull();
    expect(screen.queryByText('Виды транспорта:')).toBeNull();
    expect(screen.queryByText('Проездной:')).toBeNull();
    expect(screen.queryByText('⇅ Поменять местами')).toBeNull();
    expect(screen.queryByText('✕ Очистить точки')).toBeNull();
    expect(screen.getByDisplayValue('Старт')).toBeTruthy();
    expect(screen.getByDisplayValue('Финиш')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Найти маршруты' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Подробно' })).toBeTruthy();
  });

  it('toggles back to the full view', () => {
    render(<SearchPanel {...baseProps} />);

    fireEvent.click(screen.getByRole('button', { name: 'Компактно' }));
    fireEvent.click(screen.getByRole('button', { name: 'Подробно' }));

    expect(screen.getByText('Виды транспорта:')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Компактно' })).toBeTruthy();
  });
});

describe('SearchPanel clear points button', () => {
  it('clears both point fields and calls onClearPoints', () => {
    render(<SearchPanel {...baseProps} />);

    fireEvent.click(screen.getByRole('button', { name: '✕ Очистить точки' }));

    expect(baseProps.onClearPoints).toHaveBeenCalledTimes(1);
    expect(screen.queryByDisplayValue('Старт')).toBeNull();
    expect(screen.queryByDisplayValue('Финиш')).toBeNull();
    expect(screen.getByRole('button', { name: 'Найти маршруты' })).toBeTruthy();
  });
});

afterEach(() => cleanup());
