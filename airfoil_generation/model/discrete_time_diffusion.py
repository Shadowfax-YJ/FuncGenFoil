from copy import deepcopy
from functools import partial
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm


class EMA:
    def __init__(self, decay):
        self.decay = decay

    def update_average(self, old, new):
        if old is None:
            return new
        return old * self.decay + (1 - self.decay) * new

    def update_model_average(self, ema_model, current_model):
        for current_params, ema_params in zip(
            current_model.parameters(), ema_model.parameters()
        ):
            old, new = ema_params.data, current_params.data
            ema_params.data = self.update_average(old, new)


def generate_linear_schedule(T, low, high):
    return np.linspace(low, high, T)


def generate_cosine_schedule(T, s=0.008):
    def f(t, T):
        return (np.cos((t / T + s) / (1 + s) * np.pi / 2)) ** 2

    alphas = []
    f0 = f(0, T)

    for t in range(T + 1):
        alphas.append(f(t, T) / f0)

    betas = []

    for t in range(1, T + 1):
        betas.append(min(1 - alphas[t] / alphas[t - 1], 0.999))

    return np.array(betas)


def generate_schedule(args):
    if args.schedule == "cosine":
        betas = generate_cosine_schedule(args.num_timesteps)
    else:
        betas = generate_linear_schedule(
            args.num_timesteps,
            args.schedule_low * 1000 / args.num_timesteps,
            args.schedule_high * 1000 / args.num_timesteps,
        )

    return betas


def extract(a, t, x_shape):
    b, *_ = t.shape
    out = a.gather(-1, t)
    return out.reshape(b, *((1,) * (len(x_shape) - 1)))


class PointDiTDiffusion(nn.Module):
    __doc__ = r"""Gaussian Diffusion model. Forwarding through the module returns diffusion reversal scalar loss tensor.

    Input:
        x: tensor of shape (N, img_channels, *img_size)
        y: tensor of shape (N)
    Output:
        scalar loss tensor
    Args:
        model (nn.Module): model which estimates diffusion noise
        img_size (tuple): image size tuple (H, W)
        img_channels (int): number of image channels
        betas (np.ndarray): numpy array of diffusion betas
        loss_type (string): loss type, "l1" or "l2"
        ema_decay (float): model weights exponential moving average decay
        ema_start (int): number of steps before EMA
        ema_update_rate (int): number of steps before each EMA update
    """

    def __init__(
        self,
        model,
        latent_size,
        channels,
        betas,
        loss_type="l1",
        ema_decay=0.9999,
        ema_start=5000,
        ema_update_rate=1,
    ):
        super().__init__()

        self.model = model
        self.ema_model = deepcopy(model)

        self.ema = EMA(ema_decay)
        self.ema_decay = ema_decay
        self.ema_start = ema_start
        self.ema_update_rate = ema_update_rate
        self.step = 0

        self.latent_size = latent_size
        self.channels = channels

        if loss_type not in ["l1", "l2"]:
            raise ValueError("__init__() got unknown loss type")

        self.loss_type = loss_type
        self.num_timesteps = len(betas)
        self.ddim_timesteps = 50
        self.ddim_eta = 0

        alphas = 1.0 - betas
        alphas_cumprod = np.cumprod(alphas)

        to_torch = partial(torch.tensor, dtype=torch.float32)

        self.register_buffer("betas", to_torch(betas))
        self.register_buffer("alphas", to_torch(alphas))
        self.register_buffer("alphas_cumprod", to_torch(alphas_cumprod))

        self.register_buffer("sqrt_alphas_cumprod", to_torch(np.sqrt(alphas_cumprod)))
        self.register_buffer(
            "sqrt_one_minus_alphas_cumprod", to_torch(np.sqrt(1 - alphas_cumprod))
        )
        self.register_buffer("reciprocal_sqrt_alphas", to_torch(np.sqrt(1 / alphas)))

        self.register_buffer(
            "remove_noise_coeff", to_torch(betas / np.sqrt(1 - alphas_cumprod))
        )
        self.register_buffer("sigma", to_torch(np.sqrt(betas)))

    def update_ema(self):
        self.step += 1
        if self.step % self.ema_update_rate == 0:
            if self.step < self.ema_start:
                self.ema_model.load_state_dict(self.model.state_dict())
            else:
                self.ema.update_model_average(self.ema_model, self.model)

    @torch.no_grad()
    def remove_noise(self, x, t, y, use_ema=True):
        if use_ema:
            return (
                x
                - extract(self.remove_noise_coeff, t, x.shape) * self.ema_model(x, t, y)
            ) * extract(self.reciprocal_sqrt_alphas, t, x.shape)
        else:
            return (
                x - extract(self.remove_noise_coeff, t, x.shape) * self.model(x, t, y)
            ) * extract(self.reciprocal_sqrt_alphas, t, x.shape)

    @torch.no_grad()
    def sample_ddim(
        self,
        batch_size,
        device,
        y=None,
        use_ema=True,
        clip_denoised=True,
        gt=None,
        fix_indices=None,
    ):
        if y is not None and batch_size != len(y):
            raise ValueError("sample batch size different from length of given y")
        ddim_timesteps = self.ddim_timesteps
        c = self.num_timesteps // ddim_timesteps
        ddim_timestep_seq = np.asarray(list(range(0, self.num_timesteps, c)))

        # add one to get the final alpha values right (the ones from first scale to data during sampling)
        ddim_timestep_seq = ddim_timestep_seq + 1
        # previous sequence
        ddim_timestep_prev_seq = np.append(np.array([0]), ddim_timestep_seq[:-1])

        # start from pure noise (for each example in the batch)
        x = torch.randn(batch_size, self.latent_size, self.channels, device=device)
        for i in tqdm(
            reversed(range(0, ddim_timesteps)),
            desc="sampling loop time step",
            total=ddim_timesteps,
        ):
            t = torch.full(
                (batch_size,), ddim_timestep_seq[i], device=device, dtype=torch.long
            )
            prev_t = torch.full(
                (batch_size,),
                ddim_timestep_prev_seq[i],
                device=device,
                dtype=torch.long,
            )

            # 1. get current and previous alpha_cumprod
            alpha_cumprod_t = extract(self.alphas_cumprod, t, x.shape)
            alpha_cumprod_t_prev = extract(self.alphas_cumprod, prev_t, x.shape)

            # 2. predict noise using model
            if use_ema:
                pred_noise = self.ema_model(x, t, y)
            else:
                pred_noise = self.model(x, t, y)

            # 3. get the predicted x_0
            pred_x0 = (
                x - torch.sqrt((1.0 - alpha_cumprod_t)) * pred_noise
            ) / torch.sqrt(alpha_cumprod_t)
            if clip_denoised:
                pred_x0 = torch.clamp(pred_x0, min=-1.0, max=1.0)

            # 4. compute variance: "sigma_t(η)" -> see formula (16)
            # σ_t = sqrt((1 − α_t−1)/(1 − α_t)) * sqrt(1 − α_t/α_t−1)
            sigmas_t = self.ddim_eta * torch.sqrt(
                (1 - alpha_cumprod_t_prev)
                / (1 - alpha_cumprod_t)
                * (1 - alpha_cumprod_t / alpha_cumprod_t_prev)
            )

            # 5. compute "direction pointing to x_t" of formula (12)
            pred_dir_xt = (
                torch.sqrt(1 - alpha_cumprod_t_prev - sigmas_t**2) * pred_noise
            )

            # 6. compute x_{t-1} of formula (12)
            x_prev = (
                torch.sqrt(alpha_cumprod_t_prev) * pred_x0
                + pred_dir_xt
                + sigmas_t * torch.randn_like(x)
            )
            if fix_indices is not None:
                gt_repaint = self.perturb_x(gt, t - 1, torch.randn_like(gt))
                x_prev[:, fix_indices, 0] = gt_repaint[:, fix_indices, 0]

            x = x_prev

        return x.detach().cpu()

    @torch.no_grad()
    def sample_ddim_sequence(
        self, batch_size, device, y=None, use_ema=True, clip_denoised=True
    ):
        if y is not None and batch_size != len(y):
            raise ValueError("sample batch size different from length of given y")
        ddim_timesteps = self.ddim_timesteps
        c = self.num_timesteps // ddim_timesteps
        ddim_timestep_seq = np.asarray(list(range(0, self.num_timesteps, c)))

        # add one to get the final alpha values right (the ones from first scale to data during sampling)
        ddim_timestep_seq = ddim_timestep_seq + 1
        # previous sequence
        ddim_timestep_prev_seq = np.append(np.array([0]), ddim_timestep_seq[:-1])

        # start from pure noise (for each example in the batch)

        x = torch.randn(batch_size, self.latent_size, self.channels, device=device)
        ans = [x]
        for i in tqdm(
            reversed(range(0, ddim_timesteps)),
            desc="sampling loop time step",
            total=ddim_timesteps,
        ):
            t = torch.full(
                (batch_size,), ddim_timestep_seq[i], device=device, dtype=torch.long
            )
            prev_t = torch.full(
                (batch_size,),
                ddim_timestep_prev_seq[i],
                device=device,
                dtype=torch.long,
            )

            # 1. get current and previous alpha_cumprod
            alpha_cumprod_t = extract(self.alphas_cumprod, t, x.shape)
            alpha_cumprod_t_prev = extract(self.alphas_cumprod, prev_t, x.shape)

            # 2. predict noise using model
            if use_ema:
                pred_noise = self.ema_model(x, t, y)
            else:
                pred_noise = self.model(x, t, y)

            # 3. get the predicted x_0
            pred_x0 = (
                x - torch.sqrt((1.0 - alpha_cumprod_t)) * pred_noise
            ) / torch.sqrt(alpha_cumprod_t)
            if clip_denoised:
                pred_x0 = torch.clamp(pred_x0, min=-1.0, max=1.0)

            # 4. compute variance: "sigma_t(η)" -> see formula (16)
            # σ_t = sqrt((1 − α_t−1)/(1 − α_t)) * sqrt(1 − α_t/α_t−1)
            sigmas_t = self.ddim_eta * torch.sqrt(
                (1 - alpha_cumprod_t_prev)
                / (1 - alpha_cumprod_t)
                * (1 - alpha_cumprod_t / alpha_cumprod_t_prev)
            )

            # 5. compute "direction pointing to x_t" of formula (12)
            pred_dir_xt = (
                torch.sqrt(1 - alpha_cumprod_t_prev - sigmas_t**2) * pred_noise
            )

            # 6. compute x_{t-1} of formula (12)
            x_prev = (
                torch.sqrt(alpha_cumprod_t_prev) * pred_x0
                + pred_dir_xt
                + sigmas_t * torch.randn_like(x)
            )

            x = x_prev
            ans.append(x)

        return ans

    @torch.no_grad()
    def sample(self, batch_size, device, y=None, use_ema=True):
        if y is not None and batch_size != len(y):
            raise ValueError("sample batch size different from length of given y")

        x = torch.randn(batch_size, self.latent_size, self.channels, device=device)

        for t in range(self.num_timesteps - 1, -1, -1):
            t_batch = torch.tensor([t], device=device).repeat(batch_size)
            x = self.remove_noise(x, t_batch, y, use_ema)

            if t > 0:
                x += extract(self.sigma, t_batch, x.shape) * torch.randn_like(x)

        return x.cpu().detach()

    @torch.no_grad()
    def sample_diffusion_sequence(self, batch_size, device, y=None, use_ema=True):
        if y is not None and batch_size != len(y):
            raise ValueError("sample batch size different from length of given y")

        x = torch.randn(batch_size, self.latent_size, self.channels, device=device)
        diffusion_sequence = [x.cpu().detach()]

        for t in range(self.num_timesteps - 1, -1, -1):
            t_batch = torch.tensor([t], device=device).repeat(batch_size)
            x = self.remove_noise(x, t_batch, y, use_ema)

            if t > 0:
                x += extract(self.sigma, t_batch, x.shape) * torch.randn_like(x)

            diffusion_sequence.append(x.cpu().detach())

        return diffusion_sequence

    def perturb_x(self, x, t, noise):
        return (
            extract(self.sqrt_alphas_cumprod, t, x.shape) * x
            + extract(self.sqrt_one_minus_alphas_cumprod, t, x.shape) * noise
        )

    def get_losses(self, x, t, y):
        noise = torch.randn_like(x)

        perturbed_x = self.perturb_x(x, t, noise)
        estimated_noise = self.model(perturbed_x, t, y)

        x_0_pred = (
            perturbed_x
            - torch.sqrt((1.0 - extract(self.alphas_cumprod, t, x.shape)))
            * estimated_noise
        ) / torch.sqrt(extract(self.alphas_cumprod, t, x.shape))

        if self.loss_type == "l1":
            loss_noise = F.l1_loss(estimated_noise, noise)
        elif self.loss_type == "l2":
            loss_noise = F.mse_loss(estimated_noise, noise)

        return loss_noise

    def forward(self, x, y):
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), device=x.device)  # (b,)
        return self.get_losses(x, t, y)
