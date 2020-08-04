import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import functools
from collections import namedtuple

# Custom
from hific.models import network
from hific.utils import math, distributions, initialization, helpers

MIN_SCALE = 0.11
MIN_LIKELIHOOD = 1e-9
MAX_LIKELIHOOD = 1e3
SMALL_HYPERLATENT_FILTERS = 192
LARGE_HYPERLATENT_FILTERS = 320

HyperInfo = namedtuple(
    "HyperInfo",
    "decoded "
    "latent_nbpp hyperlatent_nbpp total_nbpp latent_qbpp hyperlatent_qbpp total_qbpp "
    "bitstring side_bitstring",
)

class CodingModel(nn.Module):
    """
    Probability model for estimation of (cross)-entropies in the context
    of data compression. TODO: Add tensor -> string compression and
    decompression functionality.
    """

    def __init__(self, n_channels, min_likelihood=MIN_LIKELIHOOD, max_likelihood=MAX_LIKELIHOOD):
        super(CodingModel, self).__init__()
        self.n_channels = n_channels
        self.min_likelihood = float(min_likelihood)
        self.max_likelihood = float(max_likelihood)

    def _quantize(self, x, mode='noise'):
        """
        mode:       If 'noise', returns continuous relaxation of hard
                    quantization through additive uniform noise channel.
                    Otherwise perform actual quantization (through rounding).
        """
        if mode == 'noise':
            quantization_noise = torch.nn.init.uniform_(torch.zeros_like(x), -0.5, 0.5)
            x = x + quantization_noise
        elif mode == 'quantize':
            x = torch.round(x)
        else:
            raise NotImplementedError
        
        return x

    def _estimate_entropy(self, x, likelihood):
        # x: (N,C,H,W)
        EPS = 1e-9  
        input_shape = x.size()
        quotient = -np.log(2.)

        batch_size = input_shape[0]
        n_pixels = input_shape[2] * input_shape[3]

        log_likelihood = torch.log(likelihood + EPS)
        n_bits = torch.sum(log_likelihood) / (batch_size * quotient)
        bpp = n_bits / n_pixels

        return n_bits, bpp


class PriorDensity(nn.Module):
    """
    Probability model for latents y. Based on Sec. 3. of [1].
    Returns convolution of Gaussian latent density with parameterized 
    mean and variance with uniform distribution U(-1/2, 1/2).

    [1] Ballé et. al., "Variational image compression with a scale hyperprior", 
        arXiv:1802.01436 (2018).
    """

    def __init__(self, n_channels, min_likelihood=MIN_LIKELIHOOD, max_likelihood=MAX_LIKELIHOOD,
        scale_lower_bound=MIN_SCALE, **kwargs):
        super(PriorDensity, self).__init__()

        self.n_channels = n_channels
        self.min_likelihood = float(min_likelihood)
        self.max_likelihood = float(max_likelihood)
        self.scale_lower_bound = scale_lower_bound

    def standardized_CDF(self, value):
        # Gaussian
        # return 0.5 * (1. + torch.erf(value/ np.sqrt(2)))
        return 0.5 * torch.erfc(value * (-1./np.sqrt(2)))

    def likelihood(self, x, mean, scale):

        scale = torch.clamp(scale, min=self.scale_lower_bound).float()

        # Assumes 1 - CDF(x) = CDF(-x)
        x = x - mean
        x = torch.abs(x)
        cdf_upper = self.standardized_CDF((0.5 - x) / scale)
        cdf_lower = self.standardized_CDF(-(0.5 + x) / scale)
        likelihood = cdf_upper - cdf_lower
        print('LIKELIHOOD shape', likelihood.size())

        return torch.clamp(likelihood, min=self.min_likelihood, max=self.max_likelihood)


    def forward(self, x, mean, scale):
        return self.likelihood(x, mean, scale)



class HyperpriorDensity(nn.Module):
    """
    Probability model for hyper-latents z. Based on Sec. 6.1. of [1].
    Returns convolution of non-parametric hyperlatent density with uniform distribution 
    U(-1/2, 1/2).

    [1] Ballé et. al., "Variational image compression with a scale hyperprior", 
        arXiv:1802.01436 (2018).
    """

    def __init__(self, n_channels, init_scale=10, filters=(3, 3, 3), min_likelihood=MIN_LIKELIHOOD, 
        max_likelihood=MAX_LIKELIHOOD, **kwargs):
        """
        init_scale: Scaling factor determining the initial width of the
                    probability densities.
        filters:    Number of filters at each layer < K
                    of the density model. Default K=4 layers.
        """
        super(HyperpriorDensity, self).__init__()
        
        self.init_scale = float(init_scale)
        self.filters = tuple(int(f) for f in filters)
        self.min_likelihood = float(min_likelihood)
        self.max_likelihood = float(max_likelihood)

        filters = (1,) + self.filters + (1,)
        scale = self.init_scale ** (1 / (len(self.filters) + 1))

        # Define univariate density model 
        for k in range(len(self.filters)+1):
            
            # Weights
            H_init = np.log(np.expm1(1 / scale / filters[k + 1]))
            H_k = nn.Parameter(torch.ones((n_channels, filters[k+1], filters[k])))  # apply softmax for non-negativity
            torch.nn.init.constant_(H_k, H_init)
            self.register_parameter('H_{}'.format(k), H_k)

            # Scale factors
            a_k = nn.Parameter(torch.zeros((n_channels, filters[k+1], 1)))
            self.register_parameter('a_{}'.format(k), a_k)

            # Biases
            b_k = nn.Parameter(torch.zeros((n_channels, filters[k+1], 1)))
            torch.nn.init.uniform_(b_k, -0.5, 0.5)
            self.register_parameter('b_{}'.format(k), b_k)

    def cdf_logits(self, x, update_parameters=True):
        """
        Evaluate logits of the cumulative densities. 
        Independent density model for each channel.

        x:  The values at which to evaluate the cumulative densities.
            torch.Tensor - shape `(C, 1, *)`.
        """
        logits = x

        for k in range(len(self.filters)+1):
            H_k = getattr(self, 'H_{}'.format(str(k)))  # Weight
            a_k = getattr(self, 'a_{}'.format(str(k)))  # Scale
            b_k = getattr(self, 'b_{}'.format(str(k)))  # Bias

            if update_parameters is False:
                H_k, a_k, b_k = H_k.detach(), a_k.detach(), b_k.detach()
            print(H_k.size())
            print(logits.size())
            logits = torch.bmm(F.softplus(H_k), logits)  # [C,filters[k+1],B]
            logits += b_k
            logits += torch.tanh(a_k) * torch.tanh(logits)

        return logits

    def likelihood(self, x):
        """
        Expected input: (N,C,H,W)
        """
        latents = x

        # Converts latents to (C,1,*) format
        N, C, H, W = latents.size()
        latents = latents.permute(1,0,2,3)
        latents = torch.reshape(latents, (C,1,-1))

        cdf_upper = self.cdf_logits(latents + 0.5)
        cdf_lower = self.cdf_logits(latents - 0.5)

        # Numerical stability using some sigmoid identities
        # to avoid subtraction of two numbers close to 1
        sign = -torch.sign(cdf_upper + cdf_lower).detach()
        likelihood = torch.abs(
            torch.sigmoid(sign * cdf_upper) - torch.sigmoid(sign * cdf_lower))

        # Reshape to (N,C,H,W)
        likelihood = torch.reshape(likelihood, (C,N,H,W))
        likelihood = likelihood.permute(1,0,2,3)
        print('LIKELIHOOD shape', likelihood.size())

        return torch.clamp(likelihood, min=self.min_likelihood, max=self.max_likelihood)

    def forward(self, x, **kwargs):
        return self.likelihood(x)



class Hyperprior(CodingModel):
    
    def __init__(self, bottleneck_capacity=220, hyperlatent_filters=LARGE_HYPERLATENT_FILTERS, mode='large'):
        """
        Introduces probabilistic model over latents of 
        latents.

        The hyperprior over the standard latents is modelled as
        a non-parametric, fully factorized density.
        """
        super(Hyperprior, self).__init__(n_channels=bottleneck_capacity)
        
        self.bottleneck_capacity = bottleneck_capacity

        analysis_net = network.HyperpriorAnalysis
        synthesis_net = network.HyperpriorSynthesis

        if mode == 'small':
            hyperlatent_filters = SMALL_HYPERLATENT_FILTERS

        self.analysis_net = analysis_net(C=bottleneck_capacity, N=hyperlatent_filters)

        # TODO: Combine scale, loc into single network
        self.synthesis_mu = synthesis_net(C=bottleneck_capacity, N=hyperlatent_filters)
        self.synthesis_std = synthesis_net(C=bottleneck_capacity, N=hyperlatent_filters,
            final_activation='softplus')

        self.hyperlatent_likelihood = HyperpriorDensity(n_channels=hyperlatent_filters)
        self.latent_likelihood = PriorDensity(n_channels=bottleneck_capacity)

    def quantize_latents(self, values, means):
        # Latents rounded instead of additive uniform noise
        # Ignore rounding in backward pass
        values = values - means
        delta = (torch.floor(values + 0.5) - values).detach()
        values = values + delta
        values = values + means
        return values


    def forward(self, latents, **kwargs):

        hyperlatents = self.analysis_net(latents)
        
        # Mismatch b/w continuous and discrete cases?
        # Differential entropy, hyperlatents
        noisy_hyperlatents = self._quantize(hyperlatents, mode='noise')
        noisy_hyperlatent_likelihood = self.hyperlatent_likelihood(noisy_hyperlatents)
        noisy_hyperlatent_bits, noisy_hyperlatent_bpp = self._estimate_entropy(hyperlatents, 
            noisy_hyperlatent_likelihood)

        # Discrete entropy, hyperlatents
        quantized_hyperlatents = self._quantize(hyperlatents, mode='quantize')
        quantized_hyperlatent_likelihood = self.hyperlatent_likelihood(quantized_hyperlatents)
        quantized_hyperlatent_bits, quantized_hyperlatent_bpp = self._estimate_entropy(quantized_hyperlatents, 
            quantized_hyperlatent_likelihood)

        if self.training is True:
            hyperlatents_decoded = noisy_hyperlatents
        else:
            hyperlatents_decoded = quantized_hyperlatents

        latent_scales = self.synthesis_std(hyperlatents_decoded)
        latent_means = self.synthesis_mu(hyperlatents_decoded)

        # Differential entropy, latents
        noisy_latents = self._quantize(latents, mode='noise')
        noisy_latent_likelihood = self.latent_likelihood(noisy_latents, mean=latent_means,
            scale=latent_scales)
        noisy_latent_bits, noisy_latent_bpp = self._estimate_entropy(latents, 
            noisy_latent_likelihood)     

        # Discrete entropy, latents
        quantized_latents = self._quantize(latents, mode='quantize')
        quantized_latent_likelihood = self.latent_likelihood(quantized_latents, mean=latent_means,
            scale=latent_scales)
        quantized_latent_bits, quantized_latent_bpp = self._estimate_entropy(quantized_latents,
            quantized_latent_likelihood)

        if self.training is True:
            latents_decoded = self.quantize_latents(latents, latent_means)
        else:
            latents_decoder = quantized_latents

        info = HyperInfo(
            decoded=latents_decoded,
            latent_nbpp=noisy_latent_bpp,
            hyperlatent_nbpp=noisy_hyperlatent_bpp,
            total_nbpp=noisy_latent_bpp + noisy_hyperlatent_bpp,
            latent_qbpp=quantized_latent_bpp,
            hyperlatent_qbpp=quantized_hyperlatent_bpp,
            total_qbpp=quantized_latent_bpp + quantized_hyperlatent_bpp,
            bitstring=None,
            side_bitstring=None
        )

        return info
        

if __name__ == '__main__':

    C = 25
    y = torch.randn((10,C,16,16))
    hp = Hyperprior(C)
    f = hp(y)
    print('Shape of decoded latents', f.decoded.shape)