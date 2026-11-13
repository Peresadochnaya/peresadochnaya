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
    expect(screen.queryByText('Прибытие к:')).toBeNull();
    expect(screen.queryByText('Отправление в:')).toBeNull();
    expect(screen.queryByText('Пересадки:')).toBeNull();
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

describe('SearchPanel time and transfers', () => {
  it('sends the selected arrival time and transfer limit', () => {
    render(<SearchPanel {...baseProps} />);

    fireEvent.change(screen.getByLabelText('Прибытие к:'), { target: { value: '09:30' } });
    fireEvent.change(screen.getByLabelText('Пересадки:'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));

    expect(baseProps.onSearch).toHaveBeenCalledWith(
      expect.objectContaining({
        departureAt: null,
        arrivalAt: expect.any(String),
        maxTransfers: 3,
      })
    );
    expect(screen.queryByText('∞')).toBeNull();
  });

  it('sends the selected departure time', () => {
    render(<SearchPanel {...baseProps} />);

    fireEvent.change(screen.getByLabelText('Отправление в:'), { target: { value: '08:15' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));

    expect(baseProps.onSearch).toHaveBeenCalledWith(
      expect.objectContaining({
        departureAt: expect.any(String),
        arrivalAt: null,
      })
    );
  });

  it('keeps departure and arrival mutually exclusive', () => {
    render(<SearchPanel {...baseProps} />);

    fireEvent.change(screen.getByLabelText('Отправление в:'), { target: { value: '08:15' } });
    fireEvent.change(screen.getByLabelText('Прибытие к:'), { target: { value: '09:30' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));

    expect(baseProps.onSearch).toHaveBeenCalledWith(
      expect.objectContaining({
        departureAt: null,
        arrivalAt: expect.any(String),
      })
    );
  });

  it('defaults to departure now and unlimited transfers', () => {
    render(<SearchPanel {...baseProps} />);

    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));

    expect(baseProps.onSearch).toHaveBeenCalledWith(
      expect.objectContaining({ departureAt: null, arrivalAt: null, maxTransfers: null })
    );
  });

  it('sends null transfers for the unlimited option', () => {
    render(<SearchPanel {...baseProps} />);

    fireEvent.change(screen.getByLabelText('Пересадки:'), { target: { value: '4' } });
    fireEvent.change(screen.getByLabelText('Пересадки:'), { target: { value: '8' } });
    fireEvent.click(screen.getByRole('button', { name: 'Найти маршруты' }));

    expect(baseProps.onSearch).toHaveBeenCalledWith(
      expect.objectContaining({ maxTransfers: null })
    );
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
