/**
 * Unit tests for WelcomeMessage component
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { WelcomeMessage } from './WelcomeMessage';

// Mock Grafana UI
jest.mock('@grafana/ui', () => ({
  useTheme2: () => ({
    colors: {
      text: {
        primary: '#000',
        secondary: '#666',
      },
      warning: {
        main: '#ff9830',
      },
    },
  }),
}));

describe('WelcomeMessage', () => {
  it('should render the welcome greeting', () => {
    render(<WelcomeMessage />);
    expect(screen.getByText('嗨，我是')).toBeInTheDocument();
  });

  it('should render the assistant name', () => {
    render(<WelcomeMessage />);
    expect(screen.getByText('數據分析助手')).toBeInTheDocument();
  });

  it('should render the description', () => {
    render(<WelcomeMessage />);
    expect(screen.getByText('透過自然語言')).toBeInTheDocument();
    expect(screen.getByText(/查詢數據、分析趨勢/)).toBeInTheDocument();
  });

  it('should have proper styling classes', () => {
    const { container } = render(<WelcomeMessage />);
    const wrapper = container.firstChild as HTMLElement;
    expect(wrapper).toHaveClass('text-center');
    expect(wrapper).toHaveClass('animate-fadeIn');
  });

  it('should render the sparkle icon', () => {
    const { container } = render(<WelcomeMessage />);
    const svg = container.querySelector('svg');
    expect(svg).toBeInTheDocument();
  });
});

