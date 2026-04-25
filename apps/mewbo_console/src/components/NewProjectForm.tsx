import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from './ui/form';
import { Input } from './ui/input';
import { Textarea } from './ui/textarea';
import { Button } from './ui/button';

const schema = z.object({
  name: z.string().trim().min(1, 'Name is required'),
  description: z.string().optional(),
  path: z.string().optional(),
});

export type NewProjectFormValues = z.infer<typeof schema>;

interface NewProjectFormProps {
  onSubmit: (values: NewProjectFormValues) => Promise<void>;
  onCancel: () => void;
}

export function NewProjectForm({ onSubmit, onCancel }: NewProjectFormProps) {
  const [submitError, setSubmitError] = useState<string | null>(null);
  const form = useForm<NewProjectFormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: '', description: '', path: '' },
  });

  const handleSubmit = form.handleSubmit(async (values) => {
    setSubmitError(null);
    const trimmed: NewProjectFormValues = {
      name: values.name.trim(),
      description: values.description?.trim() || undefined,
      path: values.path?.trim() || undefined,
    };
    try {
      await onSubmit(trimmed);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : 'Failed to create project');
    }
  });

  const submitting = form.formState.isSubmitting;

  return (
    <Form {...form}>
      <form
        onSubmit={handleSubmit}
        className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 flex flex-col gap-3"
      >
        <h3 className="text-sm font-semibold text-[hsl(var(--foreground))]">New Project</h3>
        <FormField
          control={form.control}
          name="name"
          render={({ field }) => (
            <FormItem>
              <FormLabel className="text-xs text-[hsl(var(--muted-foreground))]">Name *</FormLabel>
              <FormControl>
                <Input {...field} placeholder="My Project" autoFocus />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="description"
          render={({ field }) => (
            <FormItem>
              <FormLabel className="text-xs text-[hsl(var(--muted-foreground))]">Description</FormLabel>
              <FormControl>
                <Textarea
                  {...field}
                  rows={2}
                  placeholder="What is this project about?"
                  className="resize-none"
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="path"
          render={({ field }) => (
            <FormItem>
              <FormLabel className="text-xs text-[hsl(var(--muted-foreground))]">Path</FormLabel>
              <FormControl>
                <Input
                  {...field}
                  placeholder="/path/to/workspace"
                  className="font-mono"
                />
              </FormControl>
              <FormDescription className="text-[10px]">
                Optional — auto-generated if empty.
              </FormDescription>
              <FormMessage />
            </FormItem>
          )}
        />
        {submitError && <p className="text-xs text-red-400">{submitError}</p>}
        <div className="flex gap-2">
          <Button type="submit" size="sm" disabled={submitting}>
            {submitting ? 'Creating…' : 'Create Project'}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={submitting}>
            Cancel
          </Button>
        </div>
      </form>
    </Form>
  );
}
